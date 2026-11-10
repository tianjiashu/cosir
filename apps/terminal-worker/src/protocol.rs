use std::io::{self, Read};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde::{Deserialize, Serialize};

pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 16 * 1024;
pub const MAX_INPUT_BYTES: usize = 64 * 1024;

/// 单次 raw 输入（人工按键 / 粘贴）解码后的字节上限。
pub const MAX_RAW_INPUT_BYTES: usize = 64 * 1024;

/// 首帧决定 worker 的模式，之后不能再换：`start_session` 起交互会话（PTY、屏幕模型、shell 集成，
/// 会话结束才退出），`start_exec` 跑一条一次性命令（管道、无 stdin，命令结束即退出）。
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlFrame {
    StartSession {
        shell: Vec<String>,
        shell_kind: String,
        cwd: String,
        integration: String,
    },
    /// `raw_args`：参数按原文拼进 Windows 命令行、不做 MSVCRT 引号转义（cmd 不认 `\"`，命令原文
    /// 必须原样交给 `/c`）；其他平台没有命令行这一层，传 true 是协议错误。
    StartExec {
        program: String,
        args: Vec<String>,
        raw_args: bool,
        cwd: String,
    },
    Write {
        input: TerminalInput,
    },
    /// 取值范围在 worker 内校验，越界回 `resize_rejected` 而不是协议错误。
    Resize {
        rows: u16,
        cols: u16,
    },
    Heartbeat,
    Shutdown,
}

/// 帧结构（类型名、字段）不合法属于 Python 侧协议缺陷，仍按协议错误终止；
/// 内容校验（控制字符、长度、base64）由 `encode_input` 负责并且非致命。
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum TerminalInput {
    /// Agent 唯一的输入：只填进提示符、绝不执行，不带回车；拒绝空串与控制字符。
    Prefill { text: String },
    /// 人工输入的原始字节：不过滤控制字符（方向键、Ctrl 组合键、粘贴都原样送达）。
    Raw { data_base64: String },
}

/// 把输入编码为写入 PTY 的字节；`Err` 表示内容被拒，调用方回 `input_rejected` 后继续运行。
/// `bracketed_paste` 是读线程已解析到的终端模式位，只决定填入文本是否被包裹。
pub fn encode_input(input: TerminalInput, bracketed_paste: bool) -> anyhow::Result<Vec<u8>> {
    match input {
        TerminalInput::Prefill { text } => {
            // worker 是独立进程输入边界：拒绝空串与控制字符，既不能回车、也不能发 ESC，保证填入
            // 只会停在提示符上。长度上限只算文本本身，包裹的 12 字节不计入。
            if text.is_empty() {
                anyhow::bail!("prefill must not be empty");
            }
            if text.chars().any(char::is_control) {
                anyhow::bail!("text contains control characters");
            }
            if text.len() > MAX_INPUT_BYTES {
                anyhow::bail!("input exceeds 64 KiB");
            }
            let text = text.into_bytes();
            if bracketed_paste {
                // 粘贴模式下 shell 把整段当一次粘贴处理，不触发用户自定义的按键绑定。
                let mut bytes = Vec::with_capacity(text.len() + 12);
                bytes.extend_from_slice(b"\x1b[200~");
                bytes.extend_from_slice(&text);
                bytes.extend_from_slice(b"\x1b[201~");
                Ok(bytes)
            } else {
                // 不包裹也安全：文本里没有控制字符（连 ESC 都没有，结束序列不可能出现），
                // 写入后只会停在提示符上，不会执行。
                Ok(text)
            }
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

/// 会话模式握手额外带的 PTY 事实；执行模式没有 PTY，不带这些字段。
#[derive(Debug, Serialize)]
pub struct PtyHandshake {
    pub pty_kind: &'static str,
    pub rows: u16,
    pub columns: u16,
}

/// worker 为什么结束：`exit` 帧唯一的结局事实，后端只按它判定终态，不从错误帧或自己的标志推断。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitReason {
    /// 命令或 shell 自己结束（带退出码或信号）。
    Exited,
    /// 后端要求停止（`shutdown` 帧或关闭 stdin），且先于命令自己结束生效。
    Stopped,
    /// 超过时限收不到心跳，worker 自行停止。
    HeartbeatTimeout,
    /// 执行模式：命令没能启动（`message` 写明原因），什么也没执行。
    StartFailed,
    /// worker 自身故障（控制帧非法、PTY 读写或等待失败），`message` 只供日志排查。
    Failed,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WorkerEvent {
    Handshake {
        instance_id: String,
        pid: u32,
        /// `session` / `exec`，与首帧一致；客户端按它校验其余字段。
        mode: &'static str,
        #[serde(flatten, skip_serializing_if = "Option::is_none")]
        pty: Option<PtyHandshake>,
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
    /// 非致命拒绝或诊断，从不代表终态（终态只有 `exit`）：`input_rejected`、`input_backpressure`、
    /// `resize_rejected`、`already_started`、`query_response_failed`、`cleanup_failed`。
    /// `message` 只供日志排查，不承载契约语义。
    Error {
        code: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<String>,
    },
    /// 握手之后的最后一个事件，每条结束路径恰好一次（只有 stdout 已断开时发不出）。退出码与信号
    /// 只在命令或 shell 确实结束时出现（`stopped` 也会带上被停下时的状态）；Windows 会话模式只有
    /// 退出码。
    Exit {
        reason: ExitReason,
        #[serde(skip_serializing_if = "Option::is_none")]
        exit_code: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        signal: Option<i32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<String>,
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
    use super::{
        encode_event, read_control_frame, ControlFrame, ExitReason, PtyHandshake, WorkerEvent,
        MAX_FRAME_BYTES,
    };
    use std::io::Cursor;

    fn frame(payload: &[u8]) -> Vec<u8> {
        let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
        frame.extend_from_slice(payload);
        frame
    }

    #[test]
    fn reads_length_prefixed_start_session_frame() {
        let payload = br#"{"type":"start_session","shell":["bash"],"shell_kind":"bash","cwd":"/tmp","integration":"off"}"#;
        let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
        frame.extend_from_slice(payload);

        let parsed = read_control_frame(&mut Cursor::new(frame))
            .expect("valid frame")
            .expect("one frame");
        match parsed {
            ControlFrame::StartSession {
                shell,
                shell_kind,
                cwd,
                integration: _,
            } => {
                assert_eq!(shell, vec!["bash"]);
                assert_eq!(shell_kind, "bash");
                assert_eq!(cwd, "/tmp");
            }
            _ => panic!("expected start_session frame"),
        }
    }

    #[test]
    fn reads_start_exec_frame_and_rejects_the_old_start_name() {
        let parsed = read_control_frame(&mut Cursor::new(frame(
            br#"{"type":"start_exec","program":"/bin/sh","args":["-c","echo hi"],"raw_args":false,"cwd":"/tmp"}"#,
        )))
        .expect("valid frame")
        .expect("one frame");
        match parsed {
            ControlFrame::StartExec {
                program,
                args,
                raw_args,
                cwd,
            } => {
                assert_eq!(program, "/bin/sh");
                assert_eq!(args, vec!["-c", "echo hi"]);
                assert!(!raw_args);
                assert_eq!(cwd, "/tmp");
            }
            _ => panic!("expected start_exec frame"),
        }
        let old = frame(br#"{"type":"start","shell":["bash"],"shell_kind":"bash","cwd":"/tmp","integration":"off"}"#);
        assert!(read_control_frame(&mut Cursor::new(old)).is_err());
    }

    #[test]
    fn handshake_carries_pty_fields_only_in_session_mode() {
        let session = encode_event(&WorkerEvent::Handshake {
            instance_id: "i".into(),
            pid: 7,
            mode: "session",
            pty: Some(PtyHandshake {
                pty_kind: "unix_pty",
                rows: 24,
                columns: 80,
            }),
        })
        .expect("event");
        assert_eq!(
            &session[4..],
            br#"{"type":"handshake","instance_id":"i","pid":7,"mode":"session","pty_kind":"unix_pty","rows":24,"columns":80}"#
        );
        let exec = encode_event(&WorkerEvent::Handshake {
            instance_id: "i".into(),
            pid: 7,
            mode: "exec",
            pty: None,
        })
        .expect("event");
        assert_eq!(
            &exec[4..],
            br#"{"type":"handshake","instance_id":"i","pid":7,"mode":"exec"}"#
        );
    }

    #[test]
    fn rejects_oversized_control_frame_before_allocation() {
        let size = (MAX_FRAME_BYTES as u32 + 1).to_be_bytes();
        let error = read_control_frame(&mut Cursor::new(size)).expect_err("oversized frame");
        assert!(error.to_string().contains("invalid control frame size"));
    }

    #[test]
    fn encodes_exit_event_with_big_endian_length_prefix() {
        let frame = encode_event(&WorkerEvent::Exit {
            reason: ExitReason::Exited,
            exit_code: Some(0),
            signal: None,
            message: None,
        })
        .expect("event");
        let size = u32::from_be_bytes(frame[..4].try_into().expect("length prefix")) as usize;
        assert_eq!(size, frame.len() - 4);
        assert_eq!(
            &frame[4..],
            br#"{"type":"exit","reason":"exited","exit_code":0}"#
        );
        let stopped = encode_event(&WorkerEvent::Exit {
            reason: ExitReason::Stopped,
            exit_code: None,
            signal: Some(15),
            message: None,
        })
        .expect("event");
        assert_eq!(
            &stopped[4..],
            br#"{"type":"exit","reason":"stopped","signal":15}"#
        );
        let failed = encode_event(&WorkerEvent::Exit {
            reason: ExitReason::StartFailed,
            exit_code: None,
            signal: None,
            message: Some("no such file".into()),
        })
        .expect("event");
        assert_eq!(
            &failed[4..],
            br#"{"type":"exit","reason":"start_failed","message":"no such file"}"#
        );
    }
}

#[cfg(test)]
mod input_tests {
    use super::*;

    fn prefill(text: &str) -> TerminalInput {
        TerminalInput::Prefill { text: text.into() }
    }

    #[test]
    fn prefill_encodes_printable_characters_with_or_without_paste_wrapping() {
        let input: TerminalInput =
            serde_json::from_str(r#"{"type":"prefill","text":":wq"}"#).unwrap();
        assert!(matches!(input, TerminalInput::Prefill { .. }));
        assert_eq!(encode_input(input, false).unwrap(), b":wq");
        assert_eq!(
            encode_input(prefill(":wq"), true).unwrap(),
            b"\x1b[200~:wq\x1b[201~"
        );
        assert!(encode_input(prefill(""), false).is_err());
        for text in ["a\nb", "a\r", "\t", "\u{1b}", "\u{85}", "q\u{3}"] {
            assert!(encode_input(prefill(text), true).is_err(), "{text:?}");
        }
        assert_eq!(
            encode_input(prefill(&"a".repeat(MAX_INPUT_BYTES)), true)
                .unwrap()
                .len(),
            MAX_INPUT_BYTES + 12
        );
        assert!(encode_input(prefill(&"a".repeat(MAX_INPUT_BYTES + 1)), false).is_err());
    }

    #[test]
    fn rejects_removed_input_kinds() {
        for json in [
            r#"{"type":"line","text":"ls"}"#,
            r#"{"type":"keys","keys":["enter"]}"#,
            r#"{"type":"text","text":"ls"}"#,
            r#"{"type":"key","key":"ctrl_c"}"#,
        ] {
            assert!(
                serde_json::from_str::<TerminalInput>(json).is_err(),
                "{json}"
            );
        }
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
            code: "cleanup_failed",
            message: None,
        })
        .unwrap();
        assert_eq!(&frame[4..], br#"{"type":"error","code":"cleanup_failed"}"#);
    }
}
