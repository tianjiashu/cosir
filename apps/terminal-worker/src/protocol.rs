use std::io::{self, Read};

use serde::{Deserialize, Serialize};

pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 16 * 1024;
pub const MAX_INPUT_BYTES: usize = 64 * 1024;
pub const PROTOCOL: &str = "terminal-worker-v1";

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlFrame {
    Start {
        shell: Vec<String>,
        shell_kind: String,
        cwd: String,
        cols: u16,
        rows: u16,
    },
    Write {
        data_base64: String,
    },
    Signal {
        signal: String,
    },
    Heartbeat,
    Shutdown,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WorkerEvent {
    Handshake {
        protocol: &'static str,
        instance_id: String,
        pid: u32,
        pty_kind: &'static str,
    },
    Output {
        data_base64: String,
    },
    Status {
        cols: u16,
        rows: u16,
    },
    Error {
        code: &'static str,
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
        let payload = br#"{"type":"start","shell":["bash"],"shell_kind":"bash","cwd":"/tmp","cols":80,"rows":24}"#;
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
                cols,
                rows,
            } => {
                assert_eq!(shell, vec!["bash"]);
                assert_eq!(shell_kind, "bash");
                assert_eq!(cwd, "/tmp");
                assert_eq!((cols, rows), (80, 24));
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
    fn encodes_event_with_big_endian_length_prefix() {
        let frame = encode_event(&WorkerEvent::Status {
            cols: 100,
            rows: 40,
        })
        .expect("event");
        let size = u32::from_be_bytes(frame[..4].try_into().expect("length prefix")) as usize;
        assert_eq!(size, frame.len() - 4);
        assert_eq!(&frame[4..], br#"{"type":"status","cols":100,"rows":40}"#);
    }
}
