//! worker 入口：读首帧定模式，再交给会话模式（`session`）或执行模式（`exec`）运行到结束。
//! 两种模式共用控制帧读线程与心跳时限。

use std::io;
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::io::{FrameWriter, SharedFrameWriter};
use crate::protocol::{read_control_frame, ControlFrame};

/// 后端每 5 秒发一次心跳；超过这个时限没收到，视为后端已不在，按关闭收尾。
pub const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(15);

/// 控制帧读线程交给各模式主循环的观察。
pub enum ControlEvent {
    Frame(ControlFrame),
    /// 后端关闭了 stdin（正常停止，或后端进程已不在）。
    BackendEof,
    Failed,
}

pub fn run(instance_id: String) -> anyhow::Result<()> {
    let output: SharedFrameWriter = Arc::new(FrameWriter::new(io::stdout()));
    // 首帧在读线程起来之前同步读：它决定模式，模式再决定读线程把观察交给谁。`Stdin` 的缓冲是
    // 进程级的，之后读线程接着读不会丢字节。
    let first = read_control_frame(&mut io::stdin().lock())?;
    match first {
        None => Ok(()),
        Some(ControlFrame::StartSession {
            shell,
            shell_kind,
            cwd,
            integration,
        }) if !shell_kind.trim().is_empty() => {
            crate::session::run(instance_id, output, shell, shell_kind, cwd, integration)
        }
        Some(ControlFrame::StartExec {
            program,
            args,
            raw_args,
            cwd,
        }) => crate::exec::run(instance_id, output, program, args, raw_args, cwd),
        Some(_) => anyhow::bail!("first terminal worker frame must be start_session or start_exec"),
    }
}

pub fn spawn_control_reader<E: From<ControlEvent> + Send + 'static>(sender: Sender<E>) {
    thread::spawn(move || {
        let stdin = io::stdin();
        let mut reader = stdin.lock();
        loop {
            let event = match read_control_frame(&mut reader) {
                Ok(Some(frame)) => ControlEvent::Frame(frame),
                Ok(None) => ControlEvent::BackendEof,
                Err(_) => ControlEvent::Failed,
            };
            let last = !matches!(event, ControlEvent::Frame(_));
            if sender.send(event.into()).is_err() || last {
                return;
            }
        }
    });
}
