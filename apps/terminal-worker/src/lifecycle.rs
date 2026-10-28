use std::time::{Duration, Instant};

const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

/// Owns the worker's terminal lifecycle decisions and the single Exit event.
///
/// Reader, child-watcher and control threads only report observations to the
/// worker loop. This arbiter keeps the real child exit code ahead of PTY EOF
/// races and prevents multiple terminal paths from publishing Exit twice.
pub struct LifecycleArbiter {
    closing: bool,
    child_exit: Option<u32>,
    pty_eof: bool,
    drain_deadline: Option<Instant>,
    exit_published: bool,
}

impl LifecycleArbiter {
    pub fn running() -> Self {
        Self {
            closing: false,
            child_exit: None,
            pty_eof: false,
            drain_deadline: None,
            exit_published: false,
        }
    }

    pub fn observe_child_exit(&mut self, code: u32) {
        if self.child_exit.is_none() {
            self.child_exit = Some(code);
        }
        self.drain_deadline
            .get_or_insert_with(|| Instant::now() + OUTPUT_DRAIN_TIMEOUT);
    }

    pub fn observe_pty_eof(&mut self) {
        self.pty_eof = true;
    }

    pub fn exit_ready(&self) -> bool {
        self.child_exit.is_some()
            && (self.pty_eof
                || self
                    .drain_deadline
                    .is_some_and(|deadline| Instant::now() >= deadline))
    }

    pub fn exit_code(&self) -> Option<u32> {
        self.child_exit
    }

    /// 只有第一次收尾生效；之后的收尾请求返回 false。
    pub fn begin_closing(&mut self) -> bool {
        !std::mem::replace(&mut self.closing, true)
    }

    pub fn publish_exit(&mut self) -> bool {
        if self.exit_published {
            return false;
        }
        self.exit_published = true;
        true
    }
}
