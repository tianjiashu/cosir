//! 执行模式：跑一条一次性命令。管道、无 stdin、无 PTY；命令结束时清理它留下的进程，读完输出、
//! 上报 `exit` 后 worker 退出。
//!
//! 结束顺序：命令退出（只观察、不回收，见 `exit_watch`）→ 按进程组清理残留（`containment`）→
//! 回收命令 → 输出管道读到 EOF（残留被结束后 EOF 立即到来；有进程脱离进程组还握着管道时，等到
//! 排空时限为止）→ 上报 `exit` → 退出。后端要求停止（取消、超时，或后端已不在）时先清理整组、连
//! 命令本身一起结束，再走同一个顺序。停止请求与命令退出谁先到由 `LifecycleArbiter` 裁决，写进
//! `exit.reason`。

use std::io::{self, Read};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::thread;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;

use crate::containment::Containment;
use crate::exit_watch::ChildExit;
use crate::io::SharedFrameWriter;
use crate::lifecycle::LifecycleArbiter;
use crate::protocol::{ControlFrame, ExitReason, WorkerEvent, MAX_OUTPUT_CHUNK_BYTES};
use crate::worker::{spawn_control_reader, ControlEvent, HEARTBEAT_TIMEOUT};

enum InternalEvent {
    Control(ControlFrame),
    BackendEof,
    ControlFailed,
    OutputEof,
    BackendOutputFailed,
    ChildExited(ChildExit),
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
            output.send(&WorkerEvent::Exit {
                reason: ExitReason::StartFailed,
                exit_code: None,
                signal: None,
                message: Some(format!("{error:#}")),
            })?;
            return Ok(());
        }
    };

    let (events_tx, events_rx) = mpsc::channel();
    spawn_control_reader(events_tx.clone());
    spawn_output_reader(reader, output.clone(), events_tx.clone());
    let reaper = spawn_child_watcher(child, events_tx);
    drive(containment, reaper, &output, &events_rx);
    Ok(())
}

/// 启动命令并建立进程组归属。
fn spawn(
    program: &str,
    args: &[String],
    raw_args: bool,
    cwd: &str,
) -> anyhow::Result<(Child, Containment, io::PipeReader)> {
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

/// 命令一侧的收尾。清理整组只做一次（命令退出与停止请求都会触发）；POSIX 上命令只被观察、没被
/// 回收，回收（`reap`）必须同时满足「已清理」与「已观察到退出」（见 `containment` 模块说明）。
/// 停止请求只清理不回收：观察线程正阻塞在 `waitid` 上，抢先回收会让它拿不到退出状态。
struct Teardown {
    containment: Containment,
    reaper: Option<Child>,
    done: bool,
}

impl Teardown {
    fn run(&mut self, output: &SharedFrameWriter) {
        if std::mem::replace(&mut self.done, true) {
            return;
        }
        if self.containment.terminate().is_err() {
            let _ = output.send(&WorkerEvent::Error {
                code: "cleanup_failed",
                message: None,
            });
        }
    }

    /// 已观察到命令退出时调用：先清理（若还没清理），再回收。
    fn reap(&mut self, output: &SharedFrameWriter) {
        self.run(output);
        if let Some(mut child) = self.reaper.take() {
            let _ = child.wait();
        }
    }

    /// 停止请求：结局由仲裁器记下（命令已先退出时不改写），随后清理。
    fn stop(
        &mut self,
        lifecycle: &mut LifecycleArbiter,
        output: &SharedFrameWriter,
        reason: ExitReason,
        message: Option<&str>,
    ) {
        lifecycle.request_stop(reason, message.map(str::to_owned));
        self.run(output);
    }
}

fn drive(
    containment: Containment,
    reaper: Option<Child>,
    output: &SharedFrameWriter,
    events: &mpsc::Receiver<InternalEvent>,
) {
    let mut lifecycle = LifecycleArbiter::running();
    let mut teardown = Teardown {
        containment,
        reaper,
        done: false,
    };
    let mut last_heartbeat = Instant::now();
    loop {
        if last_heartbeat.elapsed() >= HEARTBEAT_TIMEOUT {
            teardown.stop(&mut lifecycle, output, ExitReason::HeartbeatTimeout, None);
            last_heartbeat = Instant::now();
        }
        match events.recv_timeout(Duration::from_millis(100)) {
            Ok(InternalEvent::Control(frame)) => match frame {
                ControlFrame::Heartbeat => last_heartbeat = Instant::now(),
                ControlFrame::Shutdown => {
                    teardown.stop(&mut lifecycle, output, ExitReason::Stopped, None)
                }
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
                ControlFrame::StartSession { .. } | ControlFrame::StartExec { .. } => {
                    let _ = output.send(&WorkerEvent::Error {
                        code: "already_started",
                        message: None,
                    });
                }
            },
            Ok(InternalEvent::BackendEof) => {
                teardown.stop(&mut lifecycle, output, ExitReason::Stopped, None)
            }
            Ok(InternalEvent::ControlFailed) => teardown.stop(
                &mut lifecycle,
                output,
                ExitReason::Failed,
                Some("invalid control frame"),
            ),
            Ok(InternalEvent::BackendOutputFailed) => {
                // stdout 已断开，`exit` 发不出去：清理后直接退出（未回收的命令随 worker 退出交给 init）。
                teardown.run(output);
                return;
            }
            Ok(InternalEvent::OutputEof) => lifecycle.observe_output_eof(),
            Ok(InternalEvent::ChildExited(status)) => {
                lifecycle.observe_child_exit(status);
                // 命令正常结束也清理：它用 `&` / `nohup` 留下的进程还握着输出管道。
                teardown.reap(output);
            }
            Ok(InternalEvent::ChildWaitFailed) => {
                teardown.stop(
                    &mut lifecycle,
                    output,
                    ExitReason::Failed,
                    Some("waiting for the command failed"),
                );
                publish_exit(&mut lifecycle, output);
                return;
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                teardown.run(output);
                return;
            }
        }
        if lifecycle.exit_ready() {
            publish_exit(&mut lifecycle, output);
            return;
        }
    }
}

fn publish_exit(lifecycle: &mut LifecycleArbiter, output: &SharedFrameWriter) {
    if let Some((reason, status, message)) = lifecycle.publish_exit() {
        let _ = output.send(&WorkerEvent::Exit {
            reason,
            exit_code: status.and_then(|status| status.code),
            signal: status.and_then(|status| status.signal),
            message,
        });
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

/// POSIX：观察线程只等退出、不回收，`Child` 交回 drive 在清理之后回收（返回 `Some`）。
#[cfg(unix)]
fn spawn_child_watcher(child: Child, sender: Sender<InternalEvent>) -> Option<Child> {
    let pid = child.id();
    thread::spawn(move || {
        let event = match crate::exit_watch::wait_exit(pid) {
            Ok(status) => InternalEvent::ChildExited(status),
            Err(_) => InternalEvent::ChildWaitFailed,
        };
        let _ = sender.send(event);
    });
    Some(child)
}

/// Windows：按句柄等待并回收，没有 PID 复用问题，drive 不再持有 `Child`（返回 `None`）。
#[cfg(not(unix))]
fn spawn_child_watcher(mut child: Child, sender: Sender<InternalEvent>) -> Option<Child> {
    thread::spawn(move || {
        let event = match child.wait() {
            Ok(status) => InternalEvent::ChildExited(status.into()),
            Err(_) => InternalEvent::ChildWaitFailed,
        };
        let _ = sender.send(event);
    });
    None
}

#[cfg(all(test, unix))]
mod tests {
    use super::spawn;
    use crate::exit_watch::{wait_exit, ChildExit};
    use std::io::Read;
    use std::time::{Duration, Instant};

    fn alive(pid: libc::pid_t) -> bool {
        unsafe { libc::kill(pid, 0) == 0 }
    }

    /// 与 drive 同一顺序：观察退出 → 清理 → 回收。
    fn run_to_end(script: &str) -> (String, ChildExit, std::time::Duration) {
        let started = Instant::now();
        let (mut child, mut containment, mut reader) =
            spawn("/bin/sh", &["-c".into(), script.into()], false, "/").expect("spawn");
        let status = wait_exit(child.id()).expect("wait");
        containment.terminate().expect("cleanup");
        child.wait().expect("reap");
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
            ChildExit {
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
        assert!(
            elapsed < Duration::from_secs(2),
            "cat waited for input: {elapsed:?}"
        );
    }

    #[test]
    fn processes_left_behind_are_cleaned_and_release_the_pipe() {
        let (output, _, elapsed) =
            run_to_end("sleep 30 & a=$!; nohup sleep 30 >/dev/null 2>&1 & b=$!; echo \"$a $b\"");
        let pids: Vec<libc::pid_t> = output
            .split_whitespace()
            .map(|pid| pid.parse().expect("pid"))
            .collect();
        assert_eq!(pids.len(), 2);
        // 第一个 sleep 握着输出管道：清理之后读端才能读到 EOF，所以能读完就说明已被结束。
        assert!(
            elapsed < Duration::from_secs(3),
            "pipe held open: {elapsed:?}"
        );
        let deadline = Instant::now() + Duration::from_secs(3);
        while pids.iter().any(|pid| alive(*pid)) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(
            !pids.iter().any(|pid| alive(*pid)),
            "left-behind processes survived"
        );
    }

    #[test]
    fn a_signaled_command_reports_the_signal() {
        let (_, status, _) = run_to_end("kill -TERM $$");
        assert_eq!(
            status,
            ChildExit {
                code: None,
                signal: Some(libc::SIGTERM)
            }
        );
    }

    /// 极快结束的命令在建立进程归属时已是僵尸：归属不读首进程身份，照常建立并能清理。
    #[test]
    fn a_command_that_already_exited_still_gets_contained() {
        let mut child = std::process::Command::new("/usr/bin/true")
            .spawn()
            .expect("spawn");
        let status = wait_exit(child.id()).expect("wait");
        std::thread::sleep(Duration::from_millis(50));
        let mut containment =
            super::Containment::for_process_group(&child).expect("contain a zombie leader");
        containment.terminate().expect("cleanup");
        child.wait().expect("reap");
        assert_eq!(status.code, Some(0));
    }

    #[test]
    fn spawn_failure_is_an_error_not_a_panic() {
        assert!(spawn("/no/such/program", &[], false, "/").is_err());
        assert!(spawn("/bin/sh", &[], true, "/").is_err());
    }
}
