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
        let child = pair.slave.spawn_command(command)?;
        child
            .process_id()
            .ok_or_else(|| anyhow::anyhow!("terminal shell pid unavailable"))?;
        let process_tree = ProcessTreeGuard::attach(pair.master.as_ref(), child.as_ref())?;
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

    pub fn signal(&self, signal: &str) -> anyhow::Result<()> {
        match signal {
            "interrupt" => self.write(&[0x03]),
            "eof" => self.write(&[0x04]),
            "suspend" => {
                #[cfg(windows)]
                {
                    anyhow::bail!("suspend is not supported on Windows")
                }
                #[cfg(not(windows))]
                {
                    self.write(&[0x1a])
                }
            }
            _ => anyhow::bail!("unsupported terminal signal"),
        }
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
        process_tree.terminate(child.as_mut())
    }
}
