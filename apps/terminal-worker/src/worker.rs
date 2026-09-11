use std::io;
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;

use crate::io::{FrameWriter, SharedFrameWriter};
use crate::protocol::{
    read_control_frame, ControlFrame, WorkerEvent, MAX_INPUT_BYTES, MAX_OUTPUT_CHUNK_BYTES,
    PROTOCOL,
};
use crate::pty::PtyRuntime;

const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);
const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

enum InternalEvent {
    Control(ControlFrame),
    BackendEof,
    ControlFailed,
    PtyEof,
    ChildExited(u32),
    ChildWaitFailed,
    OutputFailed,
}

pub fn run(instance_id: String) -> anyhow::Result<()> {
    let output: SharedFrameWriter = Arc::new(FrameWriter::new(io::stdout()));
    let (events_tx, events_rx) = mpsc::channel();
    spawn_control_reader(events_tx.clone());

    let start = match events_rx.recv()? {
        InternalEvent::Control(ControlFrame::Start {
            shell,
            shell_kind,
            cwd,
            cols,
            rows,
        }) if !shell_kind.trim().is_empty() => (shell, cwd, cols, rows),
        InternalEvent::BackendEof => return Ok(()),
        _ => anyhow::bail!("first terminal worker frame must be start"),
    };

    let spawned = PtyRuntime::spawn(start.0, start.1, start.2, start.3)?;
    let runtime = spawned.runtime;
    output.send(&WorkerEvent::Handshake {
        protocol: PROTOCOL,
        instance_id,
        pid: std::process::id(),
        pty_kind: pty_kind(),
    })?;
    output.send(&WorkerEvent::Status {
        cols: start.2,
        rows: start.3,
    })?;

    spawn_pty_reader(
        spawned.reader,
        runtime.clone(),
        output.clone(),
        events_tx.clone(),
    );
    spawn_child_watcher(runtime.clone(), events_tx.clone());

    let mut last_heartbeat = Instant::now();
    let mut child_exit = None;
    let mut pty_eof = false;
    let mut drain_deadline = None;
    loop {
        if last_heartbeat.elapsed() >= HEARTBEAT_TIMEOUT {
            let _ = output.send(&WorkerEvent::Error {
                code: "HEARTBEAT_TIMEOUT",
            });
            let _ = runtime.terminate();
            let _ = output.send(&WorkerEvent::Exit { exit_code: None });
            return Ok(());
        }

        match events_rx.recv_timeout(Duration::from_millis(100)) {
            Ok(InternalEvent::Control(frame)) => match frame {
                ControlFrame::Write { data_base64 } => {
                    let data = match BASE64.decode(data_base64) {
                        Ok(data) if !data.is_empty() && data.len() <= MAX_INPUT_BYTES => data,
                        _ => {
                            let _ = output.send(&WorkerEvent::Error {
                                code: "INVALID_INPUT",
                            });
                            let _ = runtime.terminate();
                            let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                            return Ok(());
                        }
                    };
                    if runtime.write(&data).is_err() {
                        let _ = output.send(&WorkerEvent::Error {
                            code: "PTY_WRITE_FAILED",
                        });
                        let _ = runtime.terminate();
                        let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                        return Ok(());
                    }
                }
                ControlFrame::Signal { signal } => {
                    if runtime.signal(&signal).is_err() {
                        let _ = output.send(&WorkerEvent::Error {
                            code: "PTY_SIGNAL_FAILED",
                        });
                    }
                }
                ControlFrame::Heartbeat => last_heartbeat = Instant::now(),
                ControlFrame::Shutdown => {
                    let _ = runtime.terminate();
                    let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                    return Ok(());
                }
                ControlFrame::Start { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "START_ALREADY_COMPLETE",
                    });
                }
            },
            Ok(InternalEvent::BackendEof) => {
                let _ = runtime.terminate();
                let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                return Ok(());
            }
            Ok(InternalEvent::ControlFailed | InternalEvent::OutputFailed) => {
                let _ = runtime.terminate();
                let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                return Ok(());
            }
            Ok(InternalEvent::PtyEof) => {
                pty_eof = true;
            }
            Ok(InternalEvent::ChildExited(code)) => {
                child_exit = Some(code);
                drain_deadline.get_or_insert_with(|| Instant::now() + OUTPUT_DRAIN_TIMEOUT);
            }
            Ok(InternalEvent::ChildWaitFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "PTY_WAIT_FAILED",
                });
                let _ = runtime.terminate();
                let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                return Ok(());
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                let _ = runtime.terminate();
                let _ = output.send(&WorkerEvent::Exit { exit_code: None });
                return Ok(());
            }
        }

        if let Some(code) = child_exit {
            if pty_eof || drain_deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                let _ = output.send(&WorkerEvent::Exit {
                    exit_code: Some(code),
                });
                return Ok(());
            }
        }
    }
}

fn spawn_control_reader(sender: Sender<InternalEvent>) {
    thread::spawn(move || {
        let stdin = io::stdin();
        let mut reader = stdin.lock();
        loop {
            match read_control_frame(&mut reader) {
                Ok(Some(frame)) => {
                    if sender.send(InternalEvent::Control(frame)).is_err() {
                        return;
                    }
                }
                Ok(None) => {
                    let _ = sender.send(InternalEvent::BackendEof);
                    return;
                }
                Err(_) => {
                    let _ = sender.send(InternalEvent::ControlFailed);
                    return;
                }
            }
        }
    });
}

fn spawn_pty_reader(
    mut reader: Box<dyn io::Read + Send>,
    runtime: Arc<PtyRuntime>,
    output: SharedFrameWriter,
    sender: Sender<InternalEvent>,
) {
    thread::spawn(move || {
        let mut buffer = vec![0_u8; MAX_OUTPUT_CHUNK_BYTES];
        let mut query_probe = Vec::with_capacity(4);
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(InternalEvent::PtyEof);
                    return;
                }
                Ok(size) => {
                    if !respond_to_terminal_queries(&runtime, &mut query_probe, &buffer[..size]) {
                        let _ = sender.send(InternalEvent::OutputFailed);
                        return;
                    }
                    let event = WorkerEvent::Output {
                        data_base64: BASE64.encode(&buffer[..size]),
                    };
                    if output.send(&event).is_err() {
                        let _ = sender.send(InternalEvent::OutputFailed);
                        return;
                    }
                }
                Err(_) => {
                    let _ = sender.send(InternalEvent::PtyEof);
                    return;
                }
            }
        }
    });
}

fn respond_to_terminal_queries(runtime: &PtyRuntime, probe: &mut Vec<u8>, data: &[u8]) -> bool {
    const DEVICE_STATUS_QUERY: &[u8] = b"\x1b[6n";
    const DEVICE_STATUS_RESPONSE: &[u8] = b"\x1b[1;1R";

    for byte in data {
        probe.push(*byte);
        if probe.ends_with(DEVICE_STATUS_QUERY) {
            if runtime.write(DEVICE_STATUS_RESPONSE).is_err() {
                return false;
            }
            probe.clear();
        }
        while probe.len() >= DEVICE_STATUS_QUERY.len() {
            probe.remove(0);
        }
    }
    true
}

fn spawn_child_watcher(runtime: Arc<PtyRuntime>, sender: Sender<InternalEvent>) {
    thread::spawn(move || loop {
        match runtime.try_wait() {
            Ok(Some(code)) => {
                let _ = sender.send(InternalEvent::ChildExited(code));
                return;
            }
            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(_) => {
                let _ = sender.send(InternalEvent::ChildWaitFailed);
                return;
            }
        }
    });
}

#[cfg(windows)]
fn pty_kind() -> &'static str {
    "conpty"
}

#[cfg(not(windows))]
fn pty_kind() -> &'static str {
    "unix_pty"
}
