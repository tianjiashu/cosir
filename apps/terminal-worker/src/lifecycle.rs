use std::time::{Duration, Instant};

use crate::exit_watch::ChildExit;
use crate::protocol::ExitReason;

const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

/// worker 结局的唯一裁决者：只有它决定 `exit` 帧的 `reason`，并保证 `exit` 只发一次。
///
/// 读线程、退出观察线程与控制线程只把观察报给主循环，由这里按到达顺序裁决：停止请求先于子进程
/// 退出到达，结局是这次停止（`stopped` / `heartbeat_timeout` / `failed`）；子进程先退出，结局是
/// `exited`，之后到的停止请求不改写它。「谁先到」只有 worker 看得到，后端不再用自己的标志推断。
/// 输出排空：子进程退出后等输出 EOF（会话模式是 PTY，执行模式是 stdout/stderr 合并管道），
/// 最多 `OUTPUT_DRAIN_TIMEOUT`，避免真实退出状态被 EOF 竞态抢先。
pub struct LifecycleArbiter {
    stop: Option<(ExitReason, Option<String>)>,
    child_exit: Option<ChildExit>,
    output_eof: bool,
    drain_deadline: Option<Instant>,
    exit_published: bool,
}

impl LifecycleArbiter {
    pub fn running() -> Self {
        Self {
            stop: None,
            child_exit: None,
            output_eof: false,
            drain_deadline: None,
            exit_published: false,
        }
    }

    pub fn observe_child_exit(&mut self, status: ChildExit) {
        if self.child_exit.is_none() {
            self.child_exit = Some(status);
        }
        self.drain_deadline
            .get_or_insert_with(|| Instant::now() + OUTPUT_DRAIN_TIMEOUT);
    }

    pub fn observe_output_eof(&mut self) {
        self.output_eof = true;
    }

    /// 记下停止原因：只有第一次、且子进程还没退出时生效，返回是否生效（调用方据此决定是否清理）。
    pub fn request_stop(&mut self, reason: ExitReason, message: Option<String>) -> bool {
        if self.stop.is_some() || self.child_exit.is_some() {
            return false;
        }
        self.stop = Some((reason, message));
        true
    }

    pub fn exit_ready(&self) -> bool {
        self.child_exit.is_some()
            && (self.output_eof
                || self
                    .drain_deadline
                    .is_some_and(|deadline| Instant::now() >= deadline))
    }

    /// 发布唯一的 `exit`：第一次返回结局（原因、子进程状态、说明），之后返回 `None`。
    pub fn publish_exit(&mut self) -> Option<(ExitReason, Option<ChildExit>, Option<String>)> {
        if std::mem::replace(&mut self.exit_published, true) {
            return None;
        }
        let (reason, message) = self.stop.clone().unwrap_or((ExitReason::Exited, None));
        Some((reason, self.child_exit, message))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const EXITED: ChildExit = ChildExit {
        code: Some(0),
        signal: None,
    };

    #[test]
    fn a_stop_that_arrives_after_the_child_exited_does_not_rewrite_the_outcome() {
        let mut lifecycle = LifecycleArbiter::running();
        lifecycle.observe_child_exit(EXITED);
        assert!(!lifecycle.request_stop(ExitReason::Stopped, None));
        assert_eq!(
            lifecycle.publish_exit(),
            Some((ExitReason::Exited, Some(EXITED), None))
        );
        assert_eq!(lifecycle.publish_exit(), None);
    }

    #[test]
    fn the_first_stop_before_the_child_exits_is_the_outcome() {
        let mut lifecycle = LifecycleArbiter::running();
        assert!(lifecycle.request_stop(ExitReason::HeartbeatTimeout, None));
        assert!(!lifecycle.request_stop(ExitReason::Stopped, None));
        lifecycle.observe_child_exit(EXITED);
        assert_eq!(
            lifecycle.publish_exit(),
            Some((ExitReason::HeartbeatTimeout, Some(EXITED), None))
        );
    }
}
