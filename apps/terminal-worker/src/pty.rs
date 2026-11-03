use std::ffi::OsString;
use std::io::{Read, Write};
use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};

use crate::geometry::Geometry;
use crate::process_tree::ProcessTreeGuard;

/// 写入队列容量（帧）：与后端输入队列同量级，单帧不超过后端的单次输入上限（64 KiB）。
const WRITE_QUEUE_FRAMES: usize = 64;

/// 写入没有交给写线程。`Full` 是背压，只拒绝这一次；`Closed` 是写线程已因 PTY 写失败退出。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteRejected {
    Full,
    Closed,
}

impl std::fmt::Display for WriteRejected {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Full => "pty write queue is full",
            Self::Closed => "pty writer stopped",
        })
    }
}

impl std::error::Error for WriteRejected {}

pub struct PtyRuntime {
    // master 随 shell 存活：resize、前台进程组查询与 ConPTY 都依赖它。
    master: Arc<Mutex<Box<dyn MasterPty + Send>>>,
    // 写入只经专用写线程：前台程序不读输入时 PTY 只缓冲约 1 KB，同步写会卡住调用方——控制循环
    // 卡住则心跳、信号、关闭都处理不了；读线程卡住（回写查询应答）则输出不再被读走，程序写输出也
    // 阻塞、更不会读输入，形成死锁。
    writes: SyncSender<Vec<u8>>,
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
        env: &[(String, String)],
        geometry: Geometry,
    ) -> anyhow::Result<SpawnedPty> {
        if shell.is_empty() {
            anyhow::bail!("shell argv must not be empty")
        }
        let pty_system = native_pty_system();
        let pair = pty_system.openpty(pty_size(geometry))?;
        let mut command =
            CommandBuilder::from_argv(shell.into_iter().map(OsString::from).collect::<Vec<_>>());
        command.cwd(cwd);
        // 终端类型由本 worker 的屏幕内核（xterm 兼容）决定，不继承宿主进程的 TERM：
        // 桌面宿主拉起的后端通常没有 TERM，readline 会退化为 dumb 终端（横向滚动、
        // 提示符整行重画），bash 3.2 下命令行回显因此错乱并误触发输出匹配。
        command.env("TERM", "xterm-256color");
        for (key, value) in env {
            command.env(key, value);
        }
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
        let writes = spawn_writer(pair.master.take_writer()?)?;
        let runtime = Arc::new(Self {
            master: Arc::new(Mutex::new(pair.master)),
            writes,
            child: Mutex::new(child),
            process_tree: Mutex::new(process_tree),
        });
        Ok(SpawnedPty { runtime, reader })
    }

    /// 按序交给写线程，不阻塞调用方；字节真正写进 PTY 的时机取决于前台程序何时读输入。
    pub fn write(&self, data: &[u8]) -> Result<(), WriteRejected> {
        self.writes.try_send(data.to_vec()).map_err(|error| match error {
            TrySendError::Full(_) => WriteRejected::Full,
            TrySendError::Disconnected(_) => WriteRejected::Closed,
        })
    }

    /// 只调整 PTY；屏幕内核由调用方在同一把屏幕锁内同步调整，二者不能分开发生。
    pub fn resize(&self, geometry: Geometry) -> anyhow::Result<()> {
        let master = self
            .master
            .lock()
            .map_err(|_| anyhow::anyhow!("pty master lock poisoned"))?;
        master.resize(pty_size(geometry))
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

    #[cfg(windows)]
    fn send_foreground_signal(&self, signal: &str) -> anyhow::Result<TerminalControlResult> {
        if signal == "interrupt" {
            self.write(&[0x03])?;
            return Ok(TerminalControlResult::Applied);
        }
        Ok(TerminalControlResult::Unsupported(
            "windows_signal_not_supported",
        ))
    }

    #[cfg(not(any(unix, windows)))]
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

fn pty_size(geometry: Geometry) -> PtySize {
    PtySize {
        rows: geometry.rows,
        cols: geometry.cols,
        pixel_width: 0,
        pixel_height: 0,
    }
}

/// 启动写线程：按序阻塞写入 PTY；写失败即退出，之后的 `write` 得到 `Closed`。
fn spawn_writer(mut writer: Box<dyn Write + Send>) -> anyhow::Result<SyncSender<Vec<u8>>> {
    let (writes, queued) = sync_channel::<Vec<u8>>(WRITE_QUEUE_FRAMES);
    std::thread::Builder::new()
        .name("pty-writer".into())
        .spawn(move || {
            for data in queued {
                if writer.write_all(&data).and_then(|()| writer.flush()).is_err() {
                    return;
                }
            }
        })?;
    Ok(writes)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    // 目的：前台程序不读输入时写入不阻塞调用方；潜在缺陷：控制循环卡在 PTY 写上，心跳、信号、关闭全部停摆。
    #[test]
    fn write_never_blocks_when_the_foreground_program_does_not_read() {
        let spawned = PtyRuntime::spawn(
            vec!["/bin/sleep".into(), "5".into()],
            "/".into(),
            &[],
            Geometry::new(24, 80).unwrap(),
        )
        .unwrap();
        let chunk = vec![b'x'; 64 * 1024];
        let started = Instant::now();
        let mut rejected = None;
        for _ in 0..WRITE_QUEUE_FRAMES + 4 {
            if let Err(error) = spawned.runtime.write(&chunk) {
                rejected = Some(error);
                break;
            }
        }
        assert_eq!(rejected, Some(WriteRejected::Full));
        assert!(started.elapsed() < Duration::from_secs(1));
        spawned.runtime.terminate().unwrap();
    }
}
