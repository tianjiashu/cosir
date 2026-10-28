use std::io;
use std::sync::atomic::Ordering;
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;

use crate::geometry::Geometry;
use crate::io::{FrameWriter, SharedFrameWriter};
use crate::lifecycle::LifecycleArbiter;
use crate::protocol::{
    encode_input, read_control_frame, ControlFrame, KeyName, WorkerEvent, MAX_OUTPUT_CHUNK_BYTES,
};
use crate::pty::{PtyRuntime, TerminalControlResult};
use crate::shell_integration::{ShellLaunch, ShellParser};
use crate::terminal_screen::TerminalScreen;

/// 读线程推进输出与控制线程 resize 共用；持锁期间发出的帧因此保持字节流顺序。
type SharedScreen = Arc<Mutex<TerminalScreen>>;

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
    let geometry = Geometry::DEFAULT;
    let spawned = PtyRuntime::spawn(launch.argv.clone(), start.2, &launch.env, geometry)?;
    let runtime = spawned.runtime;
    let terminal_screen = TerminalScreen::new(geometry);
    let app_cursor = terminal_screen.app_cursor_flag();
    let screen: SharedScreen = Arc::new(Mutex::new(terminal_screen));
    output.send(&WorkerEvent::Handshake {
        instance_id,
        pid: std::process::id(),
        pty_kind: pty_kind(),
        rows: geometry.rows,
        columns: geometry.cols,
        capabilities: capabilities(),
    })?;
    if let Some(reason) = launch.unavailable {
        output.send(&WorkerEvent::Shell {
            event: serde_json::json!({"kind":"unavailable", "reason":reason}),
        })?;
    }
    spawn_pty_reader(
        spawned.reader,
        runtime.clone(),
        screen.clone(),
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
                message: None,
            });
            terminate_and_exit(&runtime, &output, &mut lifecycle, None);
            return Ok(());
        }

        match events_rx.recv_timeout(Duration::from_millis(100)) {
            Ok(InternalEvent::Control(frame)) => match frame {
                ControlFrame::Write { input } => {
                    // 内容被拒只影响这一次输入；会话照常运行，由 Python 侧决定如何回报。
                    let data = match encode_input(input, app_cursor.load(Ordering::Relaxed)) {
                        Ok(data) => data,
                        Err(error) => {
                            let _ = output.send(&WorkerEvent::Error {
                                code: "input_rejected",
                                message: Some(error.to_string()),
                            });
                            continue;
                        }
                    };
                    if runtime.write(&data).is_err() {
                        let _ = output.send(&WorkerEvent::Error {
                            code: "PTY_WRITE_FAILED",
                            message: None,
                        });
                        terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                        return Ok(());
                    }
                }
                ControlFrame::Resize { rows, cols } => {
                    let rejected = match Geometry::new(rows, cols) {
                        Ok(geometry) => match resize_terminal(&screen, &runtime, &output, geometry)
                        {
                            Ok(ResizeOutcome::Applied) => None,
                            Ok(ResizeOutcome::PtyFailed(message)) => Some(message),
                            Err(_) => {
                                // 屏幕帧发不出去等同 stdout 断开，与读线程的输出失败同样收尾。
                                let _ = runtime.terminate();
                                return Ok(());
                            }
                        },
                        Err(error) => Some(error.to_string()),
                    };
                    if let Some(message) = rejected {
                        let _ = output.send(&WorkerEvent::Error {
                            code: "resize_rejected",
                            message: Some(message),
                        });
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
                    terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                    return Ok(());
                }
                ControlFrame::Start { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "START_ALREADY_COMPLETE",
                        message: None,
                    });
                }
            },
            Ok(InternalEvent::BackendEof) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                return Ok(());
            }
            Ok(InternalEvent::ControlFailed) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                return Ok(());
            }
            Ok(InternalEvent::BackendOutputFailed) => {
                let _ = runtime.terminate();
                return Ok(());
            }
            Ok(InternalEvent::PtyOutputEof) => {
                lifecycle.observe_pty_eof();
            }
            Ok(InternalEvent::PtyOutputReadFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "PTY_OUTPUT_READ_FAILED",
                    message: None,
                });
                terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                return Ok(());
            }
            Ok(InternalEvent::ChildExited(code)) => {
                lifecycle.observe_child_exit(code);
            }
            Ok(InternalEvent::ChildWaitFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "PTY_WAIT_FAILED",
                    message: None,
                });
                terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                return Ok(());
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                terminate_and_exit(&runtime, &output, &mut lifecycle, None);
                return Ok(());
            }
        }

        if lifecycle.exit_ready() {
            let code = lifecycle.exit_code();
            terminate_and_exit(&runtime, &output, &mut lifecycle, code);
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
    screen: SharedScreen,
    output: SharedFrameWriter,
    sender: Sender<InternalEvent>,
    mut shell_parser: ShellParser,
) {
    thread::spawn(move || {
        let mut query_failed = false;
        let mut buffer = vec![0_u8; MAX_OUTPUT_CHUNK_BYTES];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(InternalEvent::PtyOutputEof);
                    return;
                }
                Ok(size) => {
                    // 整块持锁：resize 只能落在块与块之间，不会切开语义事件与其前置输出。
                    let Ok(mut screen) = screen.lock() else {
                        let _ = sender.send(InternalEvent::PtyOutputReadFailed);
                        return;
                    };
                    let mut start = 0;
                    for (index, byte) in buffer[..size].iter().enumerate() {
                        screen.advance(*byte);
                        for reply in screen.take_replies() {
                            if !query_failed && runtime.write(reply.as_bytes()).is_err() {
                                query_failed = true;
                                let _ = output.send(&WorkerEvent::Error {
                                    code: "PTY_QUERY_RESPONSE_FAILED",
                                    message: None,
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

enum ResizeOutcome {
    Applied,
    /// PTY 拒绝调整：屏幕内核未动，尺寸保持原样。
    PtyFailed(String),
}

/// PTY 与屏幕内核在同一临界区调整，随后移交缩行推出的历史并发布新尺寸画面。
///
/// 先调 PTY：它失败时屏幕不动，两边尺寸不会分叉；shell 收到 SIGWINCH 后的重绘
/// 要等本函数释放屏幕锁才会被读线程解析，因此一定按新尺寸处理。
/// `Err` 只表示帧写出失败（stdout 断开）。
fn resize_terminal(
    screen: &SharedScreen,
    runtime: &PtyRuntime,
    output: &SharedFrameWriter,
    geometry: Geometry,
) -> anyhow::Result<ResizeOutcome> {
    let mut screen = screen
        .lock()
        .map_err(|_| anyhow::anyhow!("terminal screen lock poisoned"))?;
    if let Err(error) = runtime.resize(geometry) {
        return Ok(ResizeOutcome::PtyFailed(format!(
            "pty resize failed: {error}"
        )));
    }
    screen.resize(geometry);
    screen.drain_history();
    publish_screen(&mut screen, output)?;
    Ok(ResizeOutcome::Applied)
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
    exit_code: Option<u32>,
) {
    if !lifecycle.begin_closing() {
        return;
    }
    if runtime.terminate().is_err() {
        let _ = output.send(&WorkerEvent::Error {
            code: "PTY_CLEANUP_FAILED",
            message: None,
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

/// 握手能力是 Python 侧判定可用输入与信号的唯一依据；按键能力与编码表同源。
fn capabilities() -> Vec<&'static str> {
    // 只上报 backend 会复查的能力：`input_keys`、`key_<name>`、`signal_*`。
    let mut capabilities = vec!["input_keys"];
    capabilities.extend(platform_signals());
    capabilities.extend(KeyName::supported().into_iter().map(KeyName::capability));
    capabilities
}

fn platform_signals() -> &'static [&'static str] {
    #[cfg(unix)]
    {
        &["signal_interrupt", "signal_suspend", "signal_eof_canonical"]
    }
    #[cfg(windows)]
    {
        &["signal_interrupt"]
    }
    #[cfg(not(any(unix, windows)))]
    {
        &[]
    }
}

#[cfg(test)]
mod tests {
    use super::capabilities;

    #[test]
    fn handshake_advertises_input_keys_and_platform_keys() {
        let capabilities = capabilities();
        for required in [
            "input_keys",
            "key_up",
            "key_enter",
            "key_ctrl_c",
        ] {
            assert!(capabilities.contains(&required), "{required}");
        }
        assert_eq!(capabilities.contains(&"key_ctrl_d"), !cfg!(windows));
        assert_eq!(capabilities.contains(&"key_ctrl_z"), cfg!(windows));
        assert!(!capabilities
            .iter()
            .any(|name| name.starts_with("input_ctrl_")));
    }
}
