//! 执行模式：跑一条一次性命令。管道、无 stdin、无 PTY；命令结束时清理它留下的进程，读完输出、
//! 上报退出状态后 worker 退出。
//!
//! 结束顺序：命令退出 → 按进程组清理残留（`containment`）→ 输出管道读到 EOF（残留被结束后 EOF
//! 立即到来；有进程脱离进程组还握着管道时，等到排空时限为止）→ 上报 `exit` → 退出。后端要求停止
//! （取消、超时，或后端已不在）时先清理整组、连命令本身一起结束，再走同一个顺序。

use std::io::{self, Read};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;

use crate::containment::Containment;
use crate::io::SharedFrameWriter;
use crate::lifecycle::LifecycleArbiter;
use crate::protocol::{ControlFrame, WorkerEvent, MAX_OUTPUT_CHUNK_BYTES};
use crate::worker::{spawn_control_reader, ControlEvent, HEARTBEAT_TIMEOUT};

/// 命令怎样结束：正常退出带退出码，被信号结束（POSIX）带信号。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecStatus {
    pub code: Option<u32>,
    pub signal: Option<i32>,
}

impl From<ExitStatus> for ExecStatus {
    fn from(status: ExitStatus) -> Self {
        #[cfg(unix)]
        let signal = std::os::unix::process::ExitStatusExt::signal(&status);
        #[cfg(not(unix))]
        let signal = None;
        // Windows 的退出码是 u32（如 0xC000013A），`code()` 按位转成 i32，这里按位还原。
        Self {
            code: status.code().map(|code| code as u32),
            signal,
        }
    }
}

enum InternalEvent {
    Control(ControlFrame),
    BackendEof,
    ControlFailed,
    OutputEof,
    BackendOutputFailed,
    ChildExited(ExecStatus),
    ChildWaitFailed,
}

impl From<ControlEvent> for InternalEvent {
    fn from(event: ControlEvent) -> Self {
        match event {
            ControlEvent::Frame(frame) => Self::Control(frame),
            ControlEvent::BackendEof => Self::BackendEof,
            ControlEvent::Failed => Self::ControlFailed,
        }
    }
}

pub fn run(
    instance_id: String,
    output: SharedFrameWriter,
    program: String,
    args: Vec<String>,
    raw_args: bool,
    cwd: String,
) -> anyhow::Result<()> {
    // 先握手再启动：启动失败（找不到程序、cwd 不存在）是这条命令的结果，要经已确认身份的连接
    // 报给后端，而不是让后端以为 worker 本身起不来。
    output.send(&WorkerEvent::Handshake {
        instance_id,
        pid: std::process::id(),
        mode: "exec",
        pty: None,
    })?;
    let (child, containment, reader) = match spawn(&program, &args, raw_args, &cwd) {
        Ok(spawned) => spawned,
        Err(error) => {
            output.send(&WorkerEvent::Error {
                code: "EXEC_START_FAILED",
                message: Some(format!("{error:#}")),
            })?;
            return Ok(());
        }
    };

    let (events_tx, events_rx) = mpsc::channel();
    spawn_control_reader(events_tx.clone());
    spawn_output_reader(reader, output.clone(), events_tx.clone());
    spawn_child_watcher(child, events_tx);
    drive(containment, &output, &events_rx);
    Ok(())
}

/// 启动命令并建立进程组归属。返回的 `Child` 只交给退出观察线程：结束命令一律经归属清理
/// （按 pid 发信号或结束 Windows 作业），不需要别处再持有它。
fn spawn(
    program: &str,
    args: &[String],
    raw_args: bool,
    cwd: &str,
) -> anyhow::Result<(std::process::Child, Containment, io::PipeReader)> {
    let mut command = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        if raw_args {
            for arg in args {
                command.raw_arg(arg);
            }
        } else {
            command.args(args);
        }
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        if raw_args {
            anyhow::bail!("raw_args is only meaningful on Windows");
        }
        command.args(args);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    // stdin 必须置空：继承下来的是 worker 的控制帧管道。stdout 与 stderr 接到同一根管道，保留
    // 两者交错的先后顺序。
    let (reader, writer) = io::pipe()?;
    command
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer);
    let mut child = command.spawn()?;
    // `Command` 还握着管道写端的副本：不放掉，命令结束后读端永远等不到 EOF。
    drop(command);
    match Containment::for_process_group(&child) {
        Ok(containment) => Ok((child, containment, reader)),
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(error)
        }
    }
}

fn drive(
    mut containment: Containment,
    output: &SharedFrameWriter,
    events: &mpsc::Receiver<InternalEvent>,
) {
    let mut lifecycle = LifecycleArbiter::<ExecStatus>::running();
    let mut cleaned = false;
    let mut cleanup = |containment: &mut Containment| {
        if std::mem::replace(&mut cleaned, true) {
            return;
        }
        if containment.terminate().is_err() {
            let _ = output.send(&WorkerEvent::Error {
                code: "EXEC_CLEANUP_FAILED",
                message: None,
            });
        }
    };
    let mut last_heartbeat = Instant::now();
    loop {
        if last_heartbeat.elapsed() >= HEARTBEAT_TIMEOUT {
            let _ = output.send(&WorkerEvent::Error {
                code: "HEARTBEAT_TIMEOUT",
                message: None,
            });
            cleanup(&mut containment);
            last_heartbeat = Instant::now();
        }
        match events.recv_timeout(Duration::from_millis(100)) {
            Ok(InternalEvent::Control(frame)) => match frame {
                ControlFrame::Heartbeat => last_heartbeat = Instant::now(),
                ControlFrame::Shutdown => cleanup(&mut containment),
                ControlFrame::Write { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "input_rejected",
                        message: Some("exec mode has no stdin".into()),
                    });
                }
                ControlFrame::Resize { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "resize_rejected",
                        message: Some("exec mode has no terminal".into()),
                    });
                }
                ControlFrame::Signal { request_id, .. } => {
                    let _ = output.send(&WorkerEvent::SignalResult {
                        request_id,
                        status: "unsupported",
                        reason: Some("exec_mode"),
                    });
                }
                ControlFrame::StartSession { .. } | ControlFrame::StartExec { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "START_ALREADY_COMPLETE",
                        message: None,
                    });
                }
            },
            Ok(InternalEvent::BackendEof) | Ok(InternalEvent::ControlFailed) => {
                cleanup(&mut containment);
            }
            Ok(InternalEvent::BackendOutputFailed) => {
                cleanup(&mut containment);
                return;
            }
            Ok(InternalEvent::OutputEof) => lifecycle.observe_output_eof(),
            Ok(InternalEvent::ChildExited(status)) => {
                lifecycle.observe_child_exit(status);
                // 命令正常结束也清理：它用 `&` / `nohup` 留下的进程还握着输出管道。
                cleanup(&mut containment);
            }
            Ok(InternalEvent::ChildWaitFailed) => {
                let _ = output.send(&WorkerEvent::Error {
                    code: "EXEC_WAIT_FAILED",
                    message: None,
                });
                cleanup(&mut containment);
                return;
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                cleanup(&mut containment);
                return;
            }
        }
        if lifecycle.exit_ready() && lifecycle.publish_exit() {
            let status = lifecycle.exit_status();
            let _ = output.send(&WorkerEvent::Exit {
                exit_code: status.and_then(|status| status.code),
                signal: status.and_then(|status| status.signal),
            });
            return;
        }
    }
}

fn spawn_output_reader(
    mut reader: io::PipeReader,
    output: SharedFrameWriter,
    sender: Sender<InternalEvent>,
) {
    thread::spawn(move || {
        let mut buffer = vec![0_u8; MAX_OUTPUT_CHUNK_BYTES];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) | Err(_) => {
                    let _ = sender.send(InternalEvent::OutputEof);
                    return;
                }
                Ok(size) => {
                    if output
                        .send(&WorkerEvent::Output {
                            data_base64: BASE64.encode(&buffer[..size]),
                        })
                        .is_err()
                    {
                        let _ = sender.send(InternalEvent::BackendOutputFailed);
                        return;
                    }
                }
            }
        }
    });
}

/// 阻塞等命令退出并回收。回收后 pid 可能被复用，归属模块按启动时间识别（见 `containment`）。
fn spawn_child_watcher(mut child: std::process::Child, sender: Sender<InternalEvent>) {
    thread::spawn(move || {
        let event = match child.wait() {
            Ok(status) => InternalEvent::ChildExited(status.into()),
            Err(_) => InternalEvent::ChildWaitFailed,
        };
        let _ = sender.send(event);
    });
}

#[cfg(all(test, unix))]
mod tests {
    use super::{spawn, ExecStatus};
    use std::io::Read;
    use std::time::{Duration, Instant};

    fn alive(pid: libc::pid_t) -> bool {
        unsafe { libc::kill(pid, 0) == 0 }
    }

    fn run_to_end(script: &str) -> (String, ExecStatus, std::time::Duration) {
        let started = Instant::now();
        let (mut child, mut containment, mut reader) =
            spawn("/bin/sh", &["-c".into(), script.into()], false, "/").expect("spawn");
        let status: ExecStatus = child.wait().expect("wait").into();
        containment.terminate().expect("cleanup");
        let mut output = String::new();
        reader.read_to_string(&mut output).expect("read");
        (output, status, started.elapsed())
    }

    #[test]
    fn merges_stdout_and_stderr_and_reports_the_exit_code() {
        let (output, status, _) = run_to_end("echo out; echo err >&2; echo out2; exit 3");
        assert_eq!(output, "out\nerr\nout2\n");
        assert_eq!(
            status,
            ExecStatus {
                code: Some(3),
                signal: None
            }
        );
    }

    #[test]
    fn stdin_is_empty_instead_of_the_control_pipe() {
        let (output, status, elapsed) = run_to_end("cat; echo done");
        assert_eq!(output, "done\n");
        assert_eq!(status.code, Some(0));
        assert!(elapsed < Duration::from_secs(2), "cat waited for input: {elapsed:?}");
    }

    #[test]
    fn processes_left_behind_are_cleaned_and_release_the_pipe() {
        let (output, _, elapsed) = run_to_end(
            "sleep 30 & a=$!; nohup sleep 30 >/dev/null 2>&1 & b=$!; echo \"$a $b\"",
        );
        let pids: Vec<libc::pid_t> = output
            .split_whitespace()
            .map(|pid| pid.parse().expect("pid"))
            .collect();
        assert_eq!(pids.len(), 2);
        // 第一个 sleep 握着输出管道：清理之后读端才能读到 EOF，所以能读完就说明已被结束。
        assert!(elapsed < Duration::from_secs(3), "pipe held open: {elapsed:?}");
        let deadline = Instant::now() + Duration::from_secs(3);
        while pids.iter().any(|pid| alive(*pid)) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!pids.iter().any(|pid| alive(*pid)), "left-behind processes survived");
    }

    #[test]
    fn a_signaled_command_reports_the_signal() {
        let (_, status, _) = run_to_end("kill -TERM $$");
        assert_eq!(
            status,
            ExecStatus {
                code: None,
                signal: Some(libc::SIGTERM)
            }
        );
    }

    #[test]
    fn spawn_failure_is_an_error_not_a_panic() {
        assert!(spawn("/no/such/program", &[], false, "/").is_err());
        assert!(spawn("/bin/sh", &[], true, "/").is_err());
    }
}
