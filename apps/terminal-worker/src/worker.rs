use std::io;
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;

use crate::io::{FrameWriter, SharedFrameWriter};
use crate::lifecycle::LifecycleArbiter;
use crate::protocol::{
    read_control_frame, ControlFrame, WorkerEvent, MAX_INPUT_BYTES, MAX_OUTPUT_CHUNK_BYTES,
    PROTOCOL,
};
use crate::pty::{PtyRuntime, TerminalControlResult};
use crate::terminal_emulator::TerminalQueryResponder;

const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);

enum InternalEvent {
    Control(ControlFrame),
    BackendEof,
    ControlFailed,
    PtyOutputEof,
    PtyOutputReadFailed,
    BackendOutputFailed,
    ChildExited(u32),
    ChildWaitFailed,
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
        }) if !shell_kind.trim().is_empty() => (shell, shell_kind, cwd),
        InternalEvent::BackendEof => return Ok(()),
        _ => anyhow::bail!("first terminal worker frame must be start"),
    };

    let shell_kind = start.1;
    let spawned = PtyRuntime::spawn(start.0, start.2)?;
    let runtime = spawned.runtime;
    output.send(&WorkerEvent::Handshake {
        protocol: PROTOCOL,
        instance_id,
        pid: std::process::id(),
        pty_kind: pty_kind(),
        capabilities: capabilities(),
    })?;
    spawn_pty_reader(
        spawned.reader,
        runtime.clone(),
        output.clone(),
        events_tx.clone(),
        TerminalQueryResponder::new(matches!(shell_kind.as_str(), "cmd" | "powershell" | "pwsh")),
    );
    spawn_child_watcher(runtime.clone(), events_tx.clone());

    let mut last_heartbeat = Instant::now();
    let mut lifecycle = LifecycleArbiter::running();
    loop {
        if last_heartbeat.elapsed() >= HEARTBEAT_TIMEOUT {
            let _ = output.send(&WorkerEvent::Error {
                code: "HEARTBEAT_TIMEOUT",
            });
            terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
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
                            terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                            return Ok(());
                        }
                    };
                    if runtime.write(&data).is_err() {
                        let _ = output.send(&WorkerEvent::Error {
                            code: "PTY_WRITE_FAILED",
                        });
                        terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                        return Ok(());
                    }
                }
                ControlFrame::Signal { request_id, signal } => match runtime.signal(&signal) {
                    Ok(TerminalControlResult::Applied) => {
                        let _ = output.send(&WorkerEvent::SignalResult {
                            request_id,
                            status: "applied",
                            reason: None,
                        });
                    }
                    Ok(TerminalControlResult::Unsupported(_reason)) => {
                        let _ = output.send(&WorkerEvent::SignalResult {
                            request_id,
                            status: "unsupported",
                            reason: Some(_reason),
                        });
                    }
                    Err(_) => {
                        let _ = output.send(&WorkerEvent::SignalResult {
                            request_id,
                            status: "failed",
                            reason: Some("signal_delivery_failed"),
                        });
                    }
                },
                ControlFrame::Heartbeat => last_heartbeat = Instant::now(),
                ControlFrame::Shutdown => {
                    terminate_and_exit(&runtime, &output, &mut lifecycle, false, None);
                    return Ok(());
                }
                ControlFrame::Start { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "START_ALREADY_COMPLETE",
                    });
                }
            },
            Ok(InternalEvent::BackendEof) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, false, None);
                return Ok(());
            }
            Ok(InternalEvent::ControlFailed) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                return Ok(());
            }
            Ok(InternalEvent::BackendOutputFailed) => {
                let _ = lifecycle.begin_closing(true);
                let _ = runtime.terminate();
                return Ok(());
            }
            Ok(InternalEvent::PtyOutputEof) => {
                lifecycle.observe_pty_eof();
            }
            Ok(InternalEvent::PtyOutputReadFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "PTY_OUTPUT_READ_FAILED",
                });
                terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                return Ok(());
            }
            Ok(InternalEvent::ChildExited(code)) => {
                lifecycle.observe_child_exit(code);
            }
            Ok(InternalEvent::ChildWaitFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "PTY_WAIT_FAILED",
                });
                terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                return Ok(());
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, true, None);
                return Ok(());
            }
        }

        if lifecycle.exit_ready() {
            let code = lifecycle.exit_code();
            terminate_and_exit(&runtime, &output, &mut lifecycle, false, code);
            return Ok(());
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
    mut query_responder: TerminalQueryResponder,
) {
    thread::spawn(move || {
        let mut buffer = vec![0_u8; MAX_OUTPUT_CHUNK_BYTES];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(InternalEvent::PtyOutputEof);
                    return;
                }
                Ok(size) => {
                    let query_response_failed = !query_responder.process(&runtime, &buffer[..size]);
                    if query_response_failed
                        && output
                            .send(&WorkerEvent::Error {
                                code: "PTY_QUERY_RESPONSE_FAILED",
                            })
                            .is_err()
                    {
                        let _ = sender.send(InternalEvent::BackendOutputFailed);
                        return;
                    }
                    let event = WorkerEvent::Output {
                        data_base64: BASE64.encode(&buffer[..size]),
                    };
                    if output.send(&event).is_err() {
                        let _ = sender.send(InternalEvent::BackendOutputFailed);
                        return;
                    }
                }
                Err(error) => {
                    let event = if pty_read_is_terminal_eof(&runtime, &error) {
                        InternalEvent::PtyOutputEof
                    } else {
                        InternalEvent::PtyOutputReadFailed
                    };
                    let _ = sender.send(event);
                    return;
                }
            }
        }
    });
}

fn terminate_and_exit(
    runtime: &PtyRuntime,
    output: &SharedFrameWriter,
    lifecycle: &mut LifecycleArbiter,
    failed: bool,
    exit_code: Option<u32>,
) {
    if !lifecycle.begin_closing(failed) {
        return;
    }
    if runtime.terminate().is_err() {
        let _ = output.send(&WorkerEvent::Error {
            code: "PTY_CLEANUP_FAILED",
        });
    }
    if lifecycle.publish_exit() {
        let _ = output.send(&WorkerEvent::Exit { exit_code });
    }
}

fn pty_read_is_terminal_eof(runtime: &PtyRuntime, error: &std::io::Error) -> bool {
    if runtime.try_wait().ok().flatten().is_some() {
        return true;
    }
    #[cfg(unix)]
    {
        return error.raw_os_error() == Some(libc::EIO);
    }
    #[cfg(windows)]
    {
        matches!(
            error.raw_os_error(),
            Some(code) if code == windows_sys::Win32::Foundation::ERROR_BROKEN_PIPE as i32
        )
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = error;
        false
    }
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

fn capabilities() -> Vec<&'static str> {
    #[cfg(unix)]
    {
        vec![
            "pty_io",
            "terminal_query_dsr",
            "signal_interrupt",
            "signal_suspend",
            "signal_eof_canonical",
        ]
    }
    #[cfg(windows)]
    {
        vec!["pty_io", "terminal_query_dsr"]
    }
    #[cfg(not(any(unix, windows)))]
    {
        vec!["pty_io", "terminal_query_dsr"]
    }
}
