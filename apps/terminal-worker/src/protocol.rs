use std::io::{self, Read};

use serde::{Deserialize, Serialize};

pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 16 * 1024;
pub const MAX_INPUT_BYTES: usize = 64 * 1024;
pub const PROTOCOL: &str = "terminal-worker-v5";

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlFrame {
    Start {
        shell: Vec<String>,
        shell_kind: String,
        cwd: String,
        integration: String,
    },
    Write {
        input: TerminalInput,
    },
    Signal {
        request_id: String,
        signal: String,
    },
    Heartbeat,
    Shutdown,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum TerminalInput {
    Line { text: String },
    Key { key: InputKey },
}
#[derive(Debug, Deserialize, Clone, Copy, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum InputKey {
    CtrlC,
    CtrlD,
    CtrlZ,
}
impl InputKey {
    // 握手与编码共用同一集合，避免声明可用却无法投递。
    pub fn supported() -> &'static [Self] {
        if cfg!(windows) {
            &[Self::CtrlC, Self::CtrlZ]
        } else {
            &[Self::CtrlC, Self::CtrlD]
        }
    }
    pub fn capability(self) -> &'static str {
        match self {
            Self::CtrlC => "input_ctrl_c",
            Self::CtrlD => "input_ctrl_d",
            Self::CtrlZ => "input_ctrl_z",
        }
    }
}

pub fn encode_input(input: TerminalInput) -> anyhow::Result<Vec<u8>> {
    match input {
        TerminalInput::Line { text } => {
            // worker 是独立进程输入边界；拒绝控制字符，不能让文本绕过按键能力。
            if text.chars().any(char::is_control) {
                anyhow::bail!("line contains control characters");
            }
            if text.len() + 1 > MAX_INPUT_BYTES {
                anyhow::bail!("input exceeds 64 KiB including Enter");
            }
            let mut bytes = text.into_bytes();
            bytes.push(13);
            Ok(bytes)
        }
        TerminalInput::Key { key } => {
            if !InputKey::supported().contains(&key) {
                anyhow::bail!("unsupported control key");
            }
            Ok(vec![match key {
                InputKey::CtrlC => 3,
                InputKey::CtrlD => 4,
                InputKey::CtrlZ => 26,
            }])
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WorkerEvent {
    Handshake {
        protocol: &'static str,
        instance_id: String,
        pid: u32,
        pty_kind: &'static str,
        rows: u16,
        columns: u16,
        capabilities: Vec<&'static str>,
    },
    Screen {
        screen: crate::terminal_screen::ScreenSnapshot,
        history: Vec<String>,
    },
    Output {
        data_base64: String,
    },
    Shell {
        event: serde_json::Value,
    },
    Error {
        code: &'static str,
    },
    SignalResult {
        request_id: String,
        status: &'static str,
        reason: Option<&'static str>,
    },
    Exit {
        exit_code: Option<u32>,
    },
}

pub fn read_control_frame<R: Read>(reader: &mut R) -> anyhow::Result<Option<ControlFrame>> {
    let mut header = [0_u8; 4];
    let first = reader.read(&mut header[..1])?;
    if first == 0 {
        return Ok(None);
    }
    reader.read_exact(&mut header[1..])?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_FRAME_BYTES {
        anyhow::bail!("invalid control frame size")
    }
    let mut payload = vec![0_u8; size];
    reader.read_exact(&mut payload)?;
    let frame = serde_json::from_slice(&payload)
        .map_err(|_| anyhow::anyhow!("invalid control frame payload"))?;
    Ok(Some(frame))
}

pub fn encode_event(event: &WorkerEvent) -> anyhow::Result<Vec<u8>> {
    let payload = serde_json::to_vec(event)?;
    if payload.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "event frame too large").into());
    }
    let size = u32::try_from(payload.len())?;
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(&size.to_be_bytes());
    frame.extend_from_slice(&payload);
    Ok(frame)
}

#[cfg(test)]
mod tests {
    use super::{encode_event, read_control_frame, ControlFrame, WorkerEvent, MAX_FRAME_BYTES};
    use std::io::Cursor;

    #[test]
    fn reads_length_prefixed_start_frame() {
        let payload = br#"{"type":"start","shell":["bash"],"shell_kind":"bash","cwd":"/tmp","integration":"off"}"#;
        let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
        frame.extend_from_slice(payload);

        let parsed = read_control_frame(&mut Cursor::new(frame))
            .expect("valid frame")
            .expect("one frame");
        match parsed {
            ControlFrame::Start {
                shell,
                shell_kind,
                cwd,
                integration: _,
            } => {
                assert_eq!(shell, vec!["bash"]);
                assert_eq!(shell_kind, "bash");
                assert_eq!(cwd, "/tmp");
            }
            _ => panic!("expected start frame"),
        }
    }

    #[test]
    fn rejects_oversized_control_frame_before_allocation() {
        let size = (MAX_FRAME_BYTES as u32 + 1).to_be_bytes();
        let error = read_control_frame(&mut Cursor::new(size)).expect_err("oversized frame");
        assert!(error.to_string().contains("invalid control frame size"));
    }

    #[test]
    fn encodes_exit_event_with_big_endian_length_prefix() {
        let frame = encode_event(&WorkerEvent::Exit { exit_code: Some(0) }).expect("event");
        let size = u32::from_be_bytes(frame[..4].try_into().expect("length prefix")) as usize;
        assert_eq!(size, frame.len() - 4);
        assert_eq!(&frame[4..], br#"{"type":"exit","exit_code":0}"#);
    }

    #[test]
    fn reads_signal_frame_with_request_id() {
        let payload = br#"{"type":"signal","request_id":"req-1","signal":"interrupt"}"#;
        let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
        frame.extend_from_slice(payload);

        let parsed = read_control_frame(&mut Cursor::new(frame))
            .expect("valid frame")
            .expect("one frame");
        match parsed {
            ControlFrame::Signal { request_id, signal } => {
                assert_eq!(request_id, "req-1");
                assert_eq!(signal, "interrupt");
            }
            _ => panic!("expected signal frame"),
        }
    }

    #[test]
    fn encodes_signal_result_event() {
        let frame = encode_event(&WorkerEvent::SignalResult {
            request_id: "req-1".to_owned(),
            status: "unsupported",
            reason: Some("platform_signal_unavailable"),
        })
        .expect("event");
        let size = u32::from_be_bytes(frame[..4].try_into().expect("length prefix")) as usize;
        assert_eq!(size, frame.len() - 4);
        assert_eq!(
            &frame[4..],
            br#"{"type":"signal_result","request_id":"req-1","status":"unsupported","reason":"platform_signal_unavailable"}"#
        );
    }
}

#[cfg(test)]
mod input_tests {
    use super::*;

    #[test]
    fn complete_line_preserves_spaces_and_literal_escapes_and_appends_one_enter() {
        let input: TerminalInput =
            serde_json::from_str(r#"{"type":"line","text":"  literal\\n  "}"#).unwrap();
        assert_eq!(encode_input(input).unwrap(), b"  literal\\n  \r");
        assert_eq!(
            encode_input(TerminalInput::Line {
                text: String::new()
            })
            .unwrap(),
            b"\r"
        );
    }

    #[test]
    fn rejects_old_inputs_newlines_control_characters_and_utf8_overflow() {
        for json in [
            r#"[{"type":"key","key":"ctrl_c"}]"#,
            r#"{"type":"text","text":"raw"}"#,
            r#"{"type":"key","key":"enter"}"#,
        ] {
            assert!(serde_json::from_str::<TerminalInput>(json).is_err());
        }
        for text in ["a\nb", "a\r", "\t", "\u{1b}", "\u{85}"] {
            assert!(encode_input(TerminalInput::Line { text: text.into() }).is_err());
        }
        assert!(encode_input(TerminalInput::Line {
            text: "中".repeat(22000)
        })
        .is_err());
        assert_eq!(
            encode_input(TerminalInput::Line {
                text: "a".repeat(MAX_INPUT_BYTES - 1)
            })
            .unwrap()
            .len(),
            MAX_INPUT_BYTES
        );
        assert!(encode_input(TerminalInput::Line {
            text: "a".repeat(MAX_INPUT_BYTES)
        })
        .is_err());
    }

    #[test]
    fn control_keys_match_advertised_platform_and_never_append_enter() {
        for key in [InputKey::CtrlC, InputKey::CtrlD, InputKey::CtrlZ] {
            let result = encode_input(TerminalInput::Key { key });
            if InputKey::supported().contains(&key) {
                assert_eq!(result.unwrap().len(), 1);
            } else {
                assert!(result.is_err());
            }
        }
        assert!(InputKey::supported().contains(&InputKey::CtrlC));
        assert_eq!(
            InputKey::supported().contains(&InputKey::CtrlZ),
            cfg!(windows)
        );
        assert_eq!(
            InputKey::supported().contains(&InputKey::CtrlD),
            !cfg!(windows)
        );
    }
}
