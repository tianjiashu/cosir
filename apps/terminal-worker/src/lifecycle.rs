use std::time::{Duration, Instant};

const OUTPUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

/// Owns the worker's terminal lifecycle decisions and the single Exit event.
///
/// Reader, child-watcher and control threads only report observations to the
/// worker loop. This arbiter keeps the real child exit status ahead of output EOF
/// races and prevents multiple terminal paths from publishing Exit twice.
/// `S` is what each mode knows about the exit: the PTY exit code in session
/// mode, code plus signal in exec mode. Output EOF is the PTY's in session mode
/// and the merged stdout/stderr pipe's in exec mode.
pub struct LifecycleArbiter<S> {
    closing: bool,
    child_exit: Option<S>,
    pty_eof: bool,
    drain_deadline: Option<Instant>,
    exit_published: bool,
}

impl<S: Copy> LifecycleArbiter<S> {
    pub fn running() -> Self {
        Self {
            closing: false,
            child_exit: None,
            pty_eof: false,
            drain_deadline: None,
            exit_published: false,
        }
    }

    pub fn observe_child_exit(&mut self, status: S) {
        if self.child_exit.is_none() {
            self.child_exit = Some(status);
        }
        self.drain_deadline
            .get_or_insert_with(|| Instant::now() + OUTPUT_DRAIN_TIMEOUT);
    }

    pub fn observe_output_eof(&mut self) {
        self.pty_eof = true;
    }

    pub fn exit_ready(&self) -> bool {
        self.child_exit.is_some()
            && (self.pty_eof
                || self
                    .drain_deadline
                    .is_some_and(|deadline| Instant::now() >= deadline))
    }

    pub fn exit_status(&self) -> Option<S> {
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
