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
    encode_input, read_control_frame, ControlFrame, InputKey, WorkerEvent, MAX_OUTPUT_CHUNK_BYTES,
    PROTOCOL,
};
use crate::pty::{PtyRuntime, TerminalControlResult};
use crate::shell_integration::{ShellLaunch, ShellParser};
use crate::terminal_screen::TerminalScreen;

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
            integration,
        }) if !shell_kind.trim().is_empty() => (shell, shell_kind, cwd, integration),
        InternalEvent::BackendEof => return Ok(()),
        _ => anyhow::bail!("first terminal worker frame must be start"),
    };

    let shell_kind = start.1;
    let launch = ShellLaunch::prepare(start.0, &shell_kind, &start.3);
    let spawned = PtyRuntime::spawn(launch.argv.clone(), start.2, &launch.env)?;
    let runtime = spawned.runtime;
    output.send(&WorkerEvent::Handshake {
        protocol: PROTOCOL,
        instance_id,
        pid: std::process::id(),
        pty_kind: pty_kind(),
        rows: crate::terminal_screen::ROWS,
        columns: crate::terminal_screen::COLS,
        capabilities: capabilities()
            .into_iter()
            .chain(InputKey::supported().iter().map(|key| key.capability()))
            .chain(["screen_snapshot", "scrollback_history"])
            .collect(),
    })?;
    if let Some(reason) = launch.unavailable {
        output.send(&WorkerEvent::Shell {
            event: serde_json::json!({"kind":"unavailable", "reason":reason}),
        })?;
    }
    spawn_pty_reader(
        spawned.reader,
        runtime.clone(),
        output.clone(),
        events_tx.clone(),
        ShellParser::new(launch.token.clone()),
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
                ControlFrame::Write { input } => {
                    let data = match encode_input(input) {
                        Ok(data) => data,
                        Err(_) => {
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
    mut shell_parser: ShellParser,
) {
    thread::spawn(move || {
        let mut screen = TerminalScreen::new();
        let mut query_failed = false;
        let mut buffer = vec![0_u8; MAX_OUTPUT_CHUNK_BYTES];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(InternalEvent::PtyOutputEof);
                    return;
                }
                Ok(size) => {
                    let mut start = 0;
                    for (index, byte) in buffer[..size].iter().enumerate() {
                        screen.advance(*byte);
                        for reply in screen.take_replies() {
                            if !query_failed && runtime.write(reply.as_bytes()).is_err() {
                                query_failed = true;
                                let _ = output.send(&WorkerEvent::Error {
                                    code: "PTY_QUERY_RESPONSE_FAILED",
                                });
                            }
                        }
                        if let Some(event) = shell_parser.advance(*byte) {
                            // 标记之前的原始字节先交付；同一块内的后续输出不能越过语义事件。
                            if output
                                .send(&WorkerEvent::Output {
                                    data_base64: BASE64.encode(&buffer[start..=index]),
                                })
                                .is_err()
                                || publish_screen(&mut screen, &output).is_err()
                                || output.send(&WorkerEvent::Shell { event }).is_err()
                            {
                                let _ = sender.send(InternalEvent::BackendOutputFailed);
                                return;
                            }
                            start = index + 1;
                        }
                    }
                    if start < size
                        && output
                            .send(&WorkerEvent::Output {
                                data_base64: BASE64.encode(&buffer[start..size]),
                            })
                            .is_err()
                    {
                        let _ = sender.send(InternalEvent::BackendOutputFailed);
                        return;
                    }
                    if publish_screen(&mut screen, &output).is_err() {
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

fn publish_screen(screen: &mut TerminalScreen, output: &SharedFrameWriter) -> anyhow::Result<()> {
    if let Some((screen, history)) = screen.take_update() {
        output.send(&WorkerEvent::Screen { screen, history })?;
    }
    Ok(())
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
        vec!["pty_io", "terminal_query_dsr", "signal_interrupt"]
    }
    #[cfg(not(any(unix, windows)))]
    {
        vec!["pty_io", "terminal_query_dsr"]
    }
}
