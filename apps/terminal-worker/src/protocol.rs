use std::io::{self, Read};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde::{Deserialize, Serialize};

pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 16 * 1024;
pub const MAX_INPUT_BYTES: usize = 64 * 1024;

/// 单次 raw 输入（人工按键 / 粘贴）解码后的字节上限。
pub const MAX_RAW_INPUT_BYTES: usize = 64 * 1024;
/// 单次 keys 输入的按键个数上限。
pub const MAX_KEYS_PER_INPUT: usize = 32;

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
    /// 取值范围在 worker 内校验，越界回 `resize_rejected` 而不是协议错误。
    Resize {
        rows: u16,
        cols: u16,
    },
    Signal {
        request_id: String,
        signal: String,
    },
    Heartbeat,
    Shutdown,
}

/// 帧结构（类型名、字段）不合法属于 Python 侧协议缺陷，仍按协议错误终止；
/// 内容校验（控制字符、长度、平台不支持的键、base64）由 `encode_input` 负责并且非致命。
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum TerminalInput {
    /// Agent 单行输入：拒绝控制字符，追加 Enter。
    Line { text: String },
    /// Agent 可打印字符：与 `Line` 同样拒绝控制字符，但不追加 Enter
    /// （全屏程序单键命令、单键确认；多出的 Enter 会落到下一个读输入的程序上）。
    Text { text: String },
    /// Agent 按键序列：按顺序编码后一次写入。
    Keys { keys: Vec<KeyName> },
    /// 人工输入的原始字节：不过滤控制字符（方向键、Ctrl 组合键、粘贴都原样送达）。
    Raw { data_base64: String },
}

#[derive(Debug, Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum KeyName {
    CtrlC,
    CtrlD,
    CtrlZ,
    Up,
    Down,
    Left,
    Right,
    Home,
    End,
    PageUp,
    PageDown,
    Delete,
    Tab,
    ShiftTab,
    Escape,
    Backspace,
    Enter,
}

const COMMON_KEYS: [KeyName; 15] = [
    KeyName::CtrlC,
    KeyName::Up,
    KeyName::Down,
    KeyName::Left,
    KeyName::Right,
    KeyName::Home,
    KeyName::End,
    KeyName::PageUp,
    KeyName::PageDown,
    KeyName::Delete,
    KeyName::Tab,
    KeyName::ShiftTab,
    KeyName::Escape,
    KeyName::Backspace,
    KeyName::Enter,
];

impl KeyName {
    /// 握手与编码共用同一集合，避免声明可用却无法投递。
    /// 平台差异只在 EOF 类控制键：Unix 为 Ctrl-D，Windows 控制台为 Ctrl-Z。
    pub fn supported() -> Vec<Self> {
        let platform = if cfg!(windows) {
            KeyName::CtrlZ
        } else {
            KeyName::CtrlD
        };
        COMMON_KEYS.iter().copied().chain([platform]).collect()
    }

    pub fn capability(self) -> &'static str {
        match self {
            Self::CtrlC => "key_ctrl_c",
            Self::CtrlD => "key_ctrl_d",
            Self::CtrlZ => "key_ctrl_z",
            Self::Up => "key_up",
            Self::Down => "key_down",
            Self::Left => "key_left",
            Self::Right => "key_right",
            Self::Home => "key_home",
            Self::End => "key_end",
            Self::PageUp => "key_page_up",
            Self::PageDown => "key_page_down",
            Self::Delete => "key_delete",
            Self::Tab => "key_tab",
            Self::ShiftTab => "key_shift_tab",
            Self::Escape => "key_escape",
            Self::Backspace => "key_backspace",
            Self::Enter => "key_enter",
        }
    }

    /// xterm 兼容编码。方向键与 Home/End 随 DECCKM（`app_cursor`）切换 `ESC [` / `ESC O`，
    /// 否则 vim、less 等开启应用光标模式的程序收不到可识别的方向键。
    fn encode(self, app_cursor: bool, out: &mut Vec<u8>) {
        let cursor = |out: &mut Vec<u8>, final_byte: u8| {
            out.extend_from_slice(if app_cursor { b"\x1bO" } else { b"\x1b[" });
            out.push(final_byte);
        };
        match self {
            Self::CtrlC => out.push(0x03),
            Self::CtrlD => out.push(0x04),
            Self::CtrlZ => out.push(0x1a),
            Self::Up => cursor(out, b'A'),
            Self::Down => cursor(out, b'B'),
            Self::Right => cursor(out, b'C'),
            Self::Left => cursor(out, b'D'),
            Self::Home => cursor(out, b'H'),
            Self::End => cursor(out, b'F'),
            Self::PageUp => out.extend_from_slice(b"\x1b[5~"),
            Self::PageDown => out.extend_from_slice(b"\x1b[6~"),
            Self::Delete => out.extend_from_slice(b"\x1b[3~"),
            Self::Tab => out.push(b'\t'),
            Self::ShiftTab => out.extend_from_slice(b"\x1b[Z"),
            Self::Escape => out.push(0x1b),
            Self::Backspace => out.push(0x7f),
            Self::Enter => out.push(b'\r'),
        }
    }
}

/// worker 是独立进程输入边界：文本拒绝控制字符，不能让 line / text 绕过按键能力。
/// `reserved` 是随后追加的字节数（Enter），计入 64 KiB 上限。
fn printable_bytes(text: String, reserved: usize) -> anyhow::Result<Vec<u8>> {
    if text.chars().any(char::is_control) {
        anyhow::bail!("text contains control characters");
    }
    if text.len() + reserved > MAX_INPUT_BYTES {
        anyhow::bail!("input exceeds 64 KiB");
    }
    Ok(text.into_bytes())
}

/// 把输入编码为写入 PTY 的字节；`Err` 表示内容被拒，调用方回 `input_rejected` 后继续运行。
pub fn encode_input(input: TerminalInput, app_cursor: bool) -> anyhow::Result<Vec<u8>> {
    match input {
        TerminalInput::Line { text } => {
            let mut bytes = printable_bytes(text, 1)?;
            bytes.push(13);
            Ok(bytes)
        }
        TerminalInput::Text { text } => {
            if text.is_empty() {
                anyhow::bail!("text must not be empty");
            }
            printable_bytes(text, 0)
        }
        TerminalInput::Keys { keys } => {
            if keys.is_empty() || keys.len() > MAX_KEYS_PER_INPUT {
                anyhow::bail!("keys must contain 1..={MAX_KEYS_PER_INPUT} entries");
            }
            let supported = KeyName::supported();
            let mut bytes = Vec::new();
            for key in keys {
                if !supported.contains(&key) {
                    anyhow::bail!("key {} is not supported on this platform", key.capability());
                }
                key.encode(app_cursor, &mut bytes);
            }
            Ok(bytes)
        }
        TerminalInput::Raw { data_base64 } => {
            // 先按编码长度粗判，避免为超大载荷分配解码缓冲。
            if data_base64.len() > MAX_RAW_INPUT_BYTES.div_ceil(3) * 4 {
                anyhow::bail!("raw input exceeds 64 KiB");
            }
            let bytes = BASE64
                .decode(data_base64.as_bytes())
                .map_err(|error| anyhow::anyhow!("raw input is not valid base64: {error}"))?;
            if bytes.is_empty() || bytes.len() > MAX_RAW_INPUT_BYTES {
                anyhow::bail!("raw input must be 1..=64 KiB");
            }
            Ok(bytes)
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WorkerEvent {
    Handshake {
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
    /// `input_rejected` / `resize_rejected` 为非致命拒绝，worker 继续运行；
    /// 其余大写错误码伴随终止。`message` 只供日志排查，不承载契约语义。
    Error {
        code: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<String>,
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

    fn keys(json: &str, app_cursor: bool) -> anyhow::Result<Vec<u8>> {
        encode_input(serde_json::from_str(json).unwrap(), app_cursor)
    }

    #[test]
    fn complete_line_preserves_spaces_and_literal_escapes_and_appends_one_enter() {
        let input: TerminalInput =
            serde_json::from_str(r#"{"type":"line","text":"  literal\\n  "}"#).unwrap();
        assert_eq!(encode_input(input, false).unwrap(), b"  literal\\n  \r");
        assert_eq!(
            encode_input(
                TerminalInput::Line {
                    text: String::new()
                },
                false
            )
            .unwrap(),
            b"\r"
        );
    }

    #[test]
    fn text_sends_printable_characters_without_enter() {
        let input: TerminalInput = serde_json::from_str(r#"{"type":"text","text":":wq"}"#).unwrap();
        assert_eq!(encode_input(input, false).unwrap(), b":wq");
        assert!(encode_input(TerminalInput::Text { text: String::new() }, false).is_err());
        for text in ["q\r", "\u{1b}", "\t"] {
            assert!(encode_input(TerminalInput::Text { text: text.into() }, false).is_err());
        }
        assert_eq!(
            encode_input(
                TerminalInput::Text {
                    text: "a".repeat(MAX_INPUT_BYTES)
                },
                false
            )
            .unwrap()
            .len(),
            MAX_INPUT_BYTES
        );
    }

    #[test]
    fn rejects_old_inputs_newlines_control_characters_and_utf8_overflow() {
        for json in [
            r#"{"type":"key","key":"ctrl_c"}"#,
            r#"{"type":"keys","keys":["f1"]}"#,
        ] {
            assert!(serde_json::from_str::<TerminalInput>(json).is_err());
        }
        for text in ["a\nb", "a\r", "\t", "\u{1b}", "\u{85}"] {
            assert!(encode_input(TerminalInput::Line { text: text.into() }, false).is_err());
        }
        assert!(encode_input(
            TerminalInput::Line {
                text: "中".repeat(22000)
            },
            false
        )
        .is_err());
        assert_eq!(
            encode_input(
                TerminalInput::Line {
                    text: "a".repeat(MAX_INPUT_BYTES - 1)
                },
                false
            )
            .unwrap()
            .len(),
            MAX_INPUT_BYTES
        );
        assert!(encode_input(
            TerminalInput::Line {
                text: "a".repeat(MAX_INPUT_BYTES)
            },
            false
        )
        .is_err());
    }

    #[test]
    fn cursor_keys_follow_app_cursor_mode_and_others_do_not() {
        let json = r#"{"type":"keys","keys":["up","down","right","left","home","end"]}"#;
        assert_eq!(
            keys(json, false).unwrap(),
            b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F"
        );
        assert_eq!(
            keys(json, true).unwrap(),
            b"\x1bOA\x1bOB\x1bOC\x1bOD\x1bOH\x1bOF"
        );
        let other = r#"{"type":"keys","keys":["page_up","page_down","delete","tab","shift_tab","escape","backspace","enter","ctrl_c"]}"#;
        let expected: &[u8] = b"\x1b[5~\x1b[6~\x1b[3~\t\x1b[Z\x1b\x7f\r\x03";
        assert_eq!(keys(other, false).unwrap(), expected);
        assert_eq!(keys(other, true).unwrap(), expected);
    }

    #[test]
    fn keys_count_and_platform_control_keys_are_enforced() {
        assert!(keys(r#"{"type":"keys","keys":[]}"#, false).is_err());
        let max = format!(
            r#"{{"type":"keys","keys":[{}]}}"#,
            vec![r#""enter""#; MAX_KEYS_PER_INPUT].join(",")
        );
        assert_eq!(keys(&max, false).unwrap().len(), MAX_KEYS_PER_INPUT);
        let over = format!(
            r#"{{"type":"keys","keys":[{}]}}"#,
            vec![r#""enter""#; MAX_KEYS_PER_INPUT + 1].join(",")
        );
        assert!(keys(&over, false).is_err());
        let ctrl_d = keys(r#"{"type":"keys","keys":["ctrl_d"]}"#, false);
        let ctrl_z = keys(r#"{"type":"keys","keys":["ctrl_z"]}"#, false);
        if cfg!(windows) {
            assert!(ctrl_d.is_err());
            assert_eq!(ctrl_z.unwrap(), b"\x1a");
        } else {
            assert_eq!(ctrl_d.unwrap(), b"\x04");
            assert!(ctrl_z.is_err());
        }
        let capabilities: Vec<_> = KeyName::supported()
            .into_iter()
            .map(KeyName::capability)
            .collect();
        assert_eq!(capabilities.len(), 16);
        assert!(capabilities.contains(&"key_ctrl_c"));
        assert_eq!(capabilities.contains(&"key_ctrl_z"), cfg!(windows));
    }

    #[test]
    fn raw_input_passes_control_bytes_and_enforces_size() {
        let data = b"ls\t\x1b[A\x03\r\n";
        let input = TerminalInput::Raw {
            data_base64: BASE64.encode(data),
        };
        assert_eq!(encode_input(input, true).unwrap(), data);
        let at_limit = TerminalInput::Raw {
            data_base64: BASE64.encode(vec![b'a'; MAX_RAW_INPUT_BYTES]),
        };
        assert_eq!(
            encode_input(at_limit, false).unwrap().len(),
            MAX_RAW_INPUT_BYTES
        );
        for data_base64 in [
            BASE64.encode(vec![b'a'; MAX_RAW_INPUT_BYTES + 1]),
            String::new(),
            "not base64!".into(),
        ] {
            assert!(encode_input(TerminalInput::Raw { data_base64 }, false).is_err());
        }
    }

    #[test]
    fn resize_frame_parses_and_negative_values_are_protocol_errors() {
        let frame: ControlFrame =
            serde_json::from_str(r#"{"type":"resize","rows":30,"cols":120}"#).unwrap();
        assert!(matches!(
            frame,
            ControlFrame::Resize {
                rows: 30,
                cols: 120
            }
        ));
        assert!(
            serde_json::from_str::<ControlFrame>(r#"{"type":"resize","rows":-1,"cols":1}"#)
                .is_err()
        );
    }

    #[test]
    fn non_fatal_error_event_carries_message_only_when_present() {
        let frame = encode_event(&WorkerEvent::Error {
            code: "input_rejected",
            message: Some("bad".into()),
        })
        .unwrap();
        assert_eq!(
            &frame[4..],
            br#"{"type":"error","code":"input_rejected","message":"bad"}"#
        );
        let frame = encode_event(&WorkerEvent::Error {
            code: "PTY_WRITE_FAILED",
            message: None,
        })
        .unwrap();
        assert_eq!(
            &frame[4..],
            br#"{"type":"error","code":"PTY_WRITE_FAILED"}"#
        );
    }
}
