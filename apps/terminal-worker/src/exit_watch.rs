//! 子进程的退出状态，以及观察退出而不回收（POSIX）。
//!
//! 进程归属清理（`containment`）按首进程的 PID 认出组号或会话号，前提是这个 PID 在清理结束前不会
//! 被复用。回收（`wait`）之后 PID 随时可能分给别的进程，所以退出观察与回收分开：这里只经
//! `waitid(WNOWAIT)` 读退出状态、让首进程以僵尸身份继续占着 PID；回收归各运行时，在清理之后做一次。
//! Windows 以句柄与 Job Object 归属，没有复用问题，退出状态直接取自回收结果。

#[cfg(unix)]
use std::io;

/// 子进程怎样结束：正常退出带退出码，被信号结束（POSIX）带信号。两者恰有其一。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChildExit {
    pub code: Option<u32>,
    pub signal: Option<i32>,
}

impl From<std::process::ExitStatus> for ChildExit {
    fn from(status: std::process::ExitStatus) -> Self {
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

/// 阻塞到子进程退出，不回收。
#[cfg(unix)]
pub fn wait_exit(pid: u32) -> io::Result<ChildExit> {
    loop {
        match observe(pid, 0) {
            Ok(Some(exit)) => return Ok(exit),
            // 不带 WNOHANG 时 waitid 只在子进程退出后返回。
            Ok(None) => continue,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

/// 子进程已退出时返回退出状态，仍在运行返回 `None`；都不回收。
#[cfg(unix)]
pub fn poll_exit(pid: u32) -> io::Result<Option<ChildExit>> {
    observe(pid, libc::WNOHANG)
}

#[cfg(unix)]
fn observe(pid: u32, extra_flags: libc::c_int) -> io::Result<Option<ChildExit>> {
    let mut info = unsafe { std::mem::zeroed::<libc::siginfo_t>() };
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            pid as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOWAIT | extra_flags,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    let (reported_pid, status) = siginfo_pid_status(&info);
    // WNOHANG 且子进程仍在运行时 waitid 返回 0、si_pid 为 0。
    if reported_pid == 0 {
        return Ok(None);
    }
    Ok(Some(if info.si_code == libc::CLD_EXITED {
        ChildExit {
            code: Some(status as u32),
            signal: None,
        }
    } else {
        // 只等 WEXITED，其余 si_code 只有 CLD_KILLED / CLD_DUMPED，status 是信号。
        ChildExit {
            code: None,
            signal: Some(status),
        }
    }))
}

#[cfg(target_os = "linux")]
fn siginfo_pid_status(info: &libc::siginfo_t) -> (libc::pid_t, libc::c_int) {
    unsafe { (info.si_pid(), info.si_status()) }
}

#[cfg(all(unix, not(target_os = "linux")))]
fn siginfo_pid_status(info: &libc::siginfo_t) -> (libc::pid_t, libc::c_int) {
    (info.si_pid, info.si_status)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::process::Command;
    use std::time::Duration;

    #[test]
    fn observes_exit_code_and_signal_without_reaping() {
        let mut exited = Command::new("/bin/sh")
            .args(["-c", "exit 3"])
            .spawn()
            .unwrap();
        let expected = ChildExit {
            code: Some(3),
            signal: None,
        };
        assert_eq!(wait_exit(exited.id()).unwrap(), expected);
        // 未回收：还能再观察一次，回收也拿到同一状态。
        assert_eq!(poll_exit(exited.id()).unwrap(), Some(expected));
        assert_eq!(exited.wait().unwrap().code(), Some(3));

        let mut running = Command::new("/bin/sleep").arg("5").spawn().unwrap();
        assert_eq!(poll_exit(running.id()).unwrap(), None);
        unsafe { libc::kill(running.id() as libc::pid_t, libc::SIGTERM) };
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(
            wait_exit(running.id()).unwrap(),
            ChildExit {
                code: None,
                signal: Some(libc::SIGTERM)
            }
        );
        running.wait().unwrap();
    }
}
