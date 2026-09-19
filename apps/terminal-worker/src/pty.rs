use std::ffi::OsString;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};

use crate::process_tree::ProcessTreeGuard;

pub struct PtyRuntime {
    // Keep the PTY master alive for the lifetime of the shell. The worker does
    // not expose runtime resize; this handle is still required by ConPTY.
    #[allow(dead_code)]
    master: Arc<Mutex<Box<dyn MasterPty + Send>>>,
    writer: Mutex<Box<dyn Write + Send>>,
    child: Mutex<Box<dyn Child + Send + Sync>>,
    process_tree: Mutex<ProcessTreeGuard>,
}

pub struct SpawnedPty {
    pub runtime: Arc<PtyRuntime>,
    pub reader: Box<dyn Read + Send>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalControlResult {
    #[allow(dead_code)]
    Applied,
    Unsupported(&'static str),
}

impl PtyRuntime {
    pub fn spawn(
        shell: Vec<String>,
        cwd: String,
        cols: u16,
        rows: u16,
    ) -> anyhow::Result<SpawnedPty> {
        if shell.is_empty() {
            anyhow::bail!("shell argv must not be empty")
        }
        if cols == 0 || rows == 0 {
            anyhow::bail!("pty dimensions must be positive")
        }
        let pty_system = native_pty_system();
        let pair = pty_system.openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })?;
        let mut command =
            CommandBuilder::from_argv(shell.into_iter().map(OsString::from).collect::<Vec<_>>());
        command.cwd(cwd);
        let mut child = pair.slave.spawn_command(command)?;
        if child.process_id().is_none() {
            let _ = child.kill();
            let _ = child.wait();
            anyhow::bail!("terminal shell pid unavailable");
        }
        let process_tree = match ProcessTreeGuard::attach(pair.master.as_ref(), child.as_ref()) {
            Ok(process_tree) => process_tree,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        drop(pair.slave);
        let reader = pair.master.try_clone_reader()?;
        let writer = pair.master.take_writer()?;
        let runtime = Arc::new(Self {
            master: Arc::new(Mutex::new(pair.master)),
            writer: Mutex::new(writer),
            child: Mutex::new(child),
            process_tree: Mutex::new(process_tree),
        });
        Ok(SpawnedPty { runtime, reader })
    }

    pub fn write(&self, data: &[u8]) -> anyhow::Result<()> {
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| anyhow::anyhow!("pty writer lock poisoned"))?;
        writer.write_all(data)?;
        writer.flush()?;
        Ok(())
    }

    pub fn signal(&self, signal: &str) -> anyhow::Result<TerminalControlResult> {
        match signal {
            "interrupt" => self.send_foreground_signal("interrupt"),
            "eof" => self.send_eof(),
            "suspend" => self.send_foreground_signal("suspend"),
            _ => anyhow::bail!("unsupported terminal signal"),
        }
    }

    #[cfg(unix)]
    fn send_foreground_signal(&self, signal: &str) -> anyhow::Result<TerminalControlResult> {
        let fd = self.master_fd()?;
        let foreground = unsafe { libc::tcgetpgrp(fd) };
        if foreground <= 0 {
            anyhow::bail!("terminal foreground process group unavailable")
        }
        let signal_number = match signal {
            "interrupt" => libc::SIGINT,
            "suspend" => libc::SIGTSTP,
            _ => anyhow::bail!("unsupported foreground signal"),
        };
        if unsafe { libc::kill(-foreground, signal_number) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(TerminalControlResult::Applied)
    }

    #[cfg(not(unix))]
    fn send_foreground_signal(&self, _signal: &str) -> anyhow::Result<TerminalControlResult> {
        Ok(TerminalControlResult::Unsupported(
            "platform_signal_unavailable",
        ))
    }

    #[cfg(unix)]
    fn send_eof(&self) -> anyhow::Result<TerminalControlResult> {
        let fd = self.master_fd()?;
        let mut attributes = unsafe { std::mem::zeroed::<libc::termios>() };
        if unsafe { libc::tcgetattr(fd, &mut attributes) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        if attributes.c_lflag & libc::ICANON == 0 {
            return Ok(TerminalControlResult::Unsupported(
                "eof_requires_canonical_mode",
            ));
        }
        let eof = attributes.c_cc[libc::VEOF] as i64;
        if eof == libc::_POSIX_VDISABLE as i64 {
            return Ok(TerminalControlResult::Unsupported(
                "eof_disabled_by_termios",
            ));
        }
        self.write(&[eof as u8])?;
        Ok(TerminalControlResult::Applied)
    }

    #[cfg(not(unix))]
    fn send_eof(&self) -> anyhow::Result<TerminalControlResult> {
        Ok(TerminalControlResult::Unsupported(
            "platform_eof_unavailable",
        ))
    }

    #[cfg(unix)]
    fn master_fd(&self) -> anyhow::Result<libc::c_int> {
        let master = self
            .master
            .lock()
            .map_err(|_| anyhow::anyhow!("pty master lock poisoned"))?;
        master
            .as_raw_fd()
            .ok_or_else(|| anyhow::anyhow!("pty master file descriptor unavailable"))
    }

    pub fn try_wait(&self) -> anyhow::Result<Option<u32>> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| anyhow::anyhow!("pty child lock poisoned"))?;
        Ok(child.try_wait()?.map(|status| status.exit_code()))
    }

    pub fn terminate(&self) -> anyhow::Result<()> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| anyhow::anyhow!("pty child lock poisoned"))?;
        let mut process_tree = self
            .process_tree
            .lock()
            .map_err(|_| anyhow::anyhow!("process tree lock poisoned"))?;
        let master = self
            .master
            .lock()
            .map_err(|_| anyhow::anyhow!("pty master lock poisoned"))?;
        process_tree.terminate(child.as_mut(), master.as_ref())
    }
}
