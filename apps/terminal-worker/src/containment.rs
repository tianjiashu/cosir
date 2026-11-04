//! 用户命令进程的归属与清理：worker 结束一条命令或一个会话时，按这里的规则找出它启动过的全部
//! 进程并结束它们。两种模式各一条归属规则，清理顺序只有一份。
//!
//! - 执行模式：命令自己的**进程组**。命令以新进程组启动，非交互 shell 没有作业控制，`&`、`nohup`
//!   起的进程都留在组里。组还有成员时组号不会被复用；组空了也就无需清理，而组长 pid 被别的进程
//!   复用时启动时间对不上，见 `live_members`。
//! - 会话模式：与 shell **同一个会话**。PTY 启动时 shell 经 `setsid` 成为会话首进程，作业控制分出
//!   的后台组、`nohup` 都留在会话里。不用另外两种看似可行的判断：
//!   - 从 shell 往下找子孙：shell 先退出时后台进程已被收养，一个都找不到。
//!   - 控制终端是本 PTY：会话首进程退出时内核把控制终端从会话上摘掉（macOS `ps` 显示 `??`、
//!     Linux `tty_nr` 归零），shell 自己退出后残留同样找不到；会话号则一直保留到最后一个成员退出。
//!
//! 主动 `setsid` 脱离会话的进程（守护化）不在范围内，留给沙箱。Windows 两种模式都用 Job Object
//! （`KILL_ON_JOB_CLOSE`），挂入后的子孙都在作业里，结束作业即可；挂入发生在启动之后，其间的
//! 短竞态接受。
//!
//! 本模块只结束成员、不回收首进程：回收（`wait`）与退出状态归各自的运行时。

#[cfg(unix)]
use std::io;
#[cfg(unix)]
use std::time::{Duration, Instant};

/// 宽限期：HUP 之后反复发 TERM，到点仍在的发 KILL；全部退出即提前结束，不白等。
#[cfg(unix)]
const GRACE: Duration = Duration::from_secs(1);
#[cfg(unix)]
const POLL: Duration = Duration::from_millis(20);

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum Membership {
    Session,
    ProcessGroup,
}

#[cfg(unix)]
pub struct Containment {
    leader: ProcessIdentity,
    membership: Membership,
}

#[cfg(windows)]
pub struct Containment {
    job: Option<WindowsJob>,
}

#[cfg(not(any(unix, windows)))]
pub struct Containment;

impl Containment {
    /// 会话模式：以刚启动的 shell 为会话首进程。shell 必须已是会话首进程（portable-pty 在启动前
    /// `setsid`），否则会话号指向的是 worker 自己的会话，清理会波及无关进程，所以直接拒绝。
    pub fn for_session(child: &dyn portable_pty::Child) -> anyhow::Result<Self> {
        #[cfg(unix)]
        {
            let pid = child
                .process_id()
                .ok_or_else(|| anyhow::anyhow!("terminal shell pid unavailable"))?;
            Self::from_leader(pid, Membership::Session)
        }

        #[cfg(windows)]
        {
            let handle = child
                .as_raw_handle()
                .ok_or_else(|| anyhow::anyhow!("terminal shell handle unavailable"))?;
            Ok(Self {
                job: Some(WindowsJob::assign(handle)?),
            })
        }

        #[cfg(not(any(unix, windows)))]
        {
            let _ = child;
            Ok(Self)
        }
    }

    /// 执行模式：以刚启动的命令为进程组组长（启动时 `process_group(0)`）。
    pub fn for_process_group(child: &std::process::Child) -> anyhow::Result<Self> {
        #[cfg(unix)]
        {
            Self::from_leader(child.id(), Membership::ProcessGroup)
        }

        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            Ok(Self {
                job: Some(WindowsJob::assign(child.as_raw_handle())?),
            })
        }

        #[cfg(not(any(unix, windows)))]
        {
            let _ = child;
            Ok(Self)
        }
    }

    #[cfg(unix)]
    fn from_leader(pid: u32, membership: Membership) -> anyhow::Result<Self> {
        let pid: libc::pid_t = pid
            .try_into()
            .map_err(|_| anyhow::anyhow!("leader pid out of range"))?;
        let leader = read_process_identity(pid)?
            .ok_or_else(|| anyhow::anyhow!("leader process identity unavailable"))?;
        let leads = match membership {
            Membership::Session => leader.session == pid,
            Membership::ProcessGroup => leader.group == pid,
        };
        if !leads {
            anyhow::bail!("process {pid} does not lead its {membership:?}")
        }
        Ok(Self { leader, membership })
    }

    /// 结束全部成员（首进程还活着时也包括它），不回收首进程。命令或会话的每条结束路径都走这里：
    /// 正常结束时清理它留下的进程，取消、超时与关闭时连首进程一起结束。
    pub fn terminate(&mut self) -> anyhow::Result<()> {
        #[cfg(unix)]
        {
            let mut failures = 0;
            let deadline = Instant::now() + GRACE;
            // 交互 shell 忽略 SIGTERM、只认 SIGHUP（终端挂断，shell 会转发给作业）：先发 HUP 才能让
            // shell 立即退出。
            let mut signal = libc::SIGHUP;
            loop {
                let Some(members) = self.live_members() else {
                    break;
                };
                if members.is_empty() {
                    break;
                }
                let last_round = Instant::now() >= deadline;
                failures += signal_all(&members, if last_round { libc::SIGKILL } else { signal });
                if last_round {
                    break;
                }
                signal = libc::SIGTERM;
                std::thread::sleep(POLL);
            }
            if failures > 0 {
                anyhow::bail!("failed to signal {failures} contained processes")
            }
            Ok(())
        }

        #[cfg(windows)]
        {
            match self.job.take() {
                Some(mut job) => job.terminate().map_err(Into::into),
                None => Ok(()),
            }
        }

        #[cfg(not(any(unix, windows)))]
        {
            Ok(())
        }
    }

    /// 仍活着的成员（不含僵尸）。组号、会话号只在最后一个成员退出后才可能被复用：若首进程的 pid
    /// 已换成另一个进程，说明归属早已结束、号码可能属于别人，返回 `None` 不再清理。
    #[cfg(unix)]
    fn live_members(&self) -> Option<Vec<ProcessIdentity>> {
        if let Ok(Some(current)) = read_process_identity(self.leader.pid) {
            if current.start_time != self.leader.start_time {
                return None;
            }
        }
        Some(
            all_pids()
                .into_iter()
                .filter_map(|pid| read_process_identity(pid).ok().flatten())
                .filter(|identity| !identity.zombie && self.contains(identity))
                .collect(),
        )
    }

    #[cfg(unix)]
    fn contains(&self, identity: &ProcessIdentity) -> bool {
        match self.membership {
            Membership::Session => identity.session == self.leader.session,
            Membership::ProcessGroup => identity.group == self.leader.group,
        }
    }
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
struct ProcessIdentity {
    pid: libc::pid_t,
    group: libc::pid_t,
    session: libc::pid_t,
    start_time: u64,
    zombie: bool,
}

#[cfg(unix)]
fn signal_all(members: &[ProcessIdentity], signal: libc::c_int) -> usize {
    members
        .iter()
        .filter(|identity| {
            let sent = unsafe { libc::kill(identity.pid, signal) } == 0;
            !sent && io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
        })
        .count()
}

#[cfg(target_os = "macos")]
fn all_pids() -> Vec<libc::pid_t> {
    // 两次调用之间可能新增进程：按返回的个数留足余量，装满了就加倍重取。
    let mut capacity = 1024;
    loop {
        let mut pids = vec![0 as libc::pid_t; capacity];
        let count = unsafe {
            libc::proc_listallpids(
                pids.as_mut_ptr().cast(),
                (pids.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int,
            )
        };
        if count <= 0 {
            return Vec::new();
        }
        let count = count as usize;
        if count < pids.len() {
            pids.truncate(count);
            return pids.into_iter().filter(|pid| *pid > 0).collect();
        }
        capacity *= 2;
    }
}

#[cfg(target_os = "macos")]
fn read_process_identity(pid: libc::pid_t) -> io::Result<Option<ProcessIdentity>> {
    let mut info = unsafe { std::mem::zeroed::<libc::proc_bsdinfo>() };
    let size = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int,
        )
    };
    if size <= 0 {
        return Ok(None);
    }
    let session = unsafe { libc::getsid(pid) };
    if session <= 0 {
        return Ok(None);
    }
    Ok(Some(ProcessIdentity {
        pid,
        group: info.pbi_pgid as libc::pid_t,
        session,
        start_time: info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec,
        zombie: info.pbi_status == libc::SZOMB,
    }))
}

#[cfg(target_os = "linux")]
fn all_pids() -> Vec<libc::pid_t> {
    std::fs::read_dir("/proc")
        .map(|entries| {
            entries
                .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse().ok())
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(target_os = "linux")]
fn read_process_identity(pid: libc::pid_t) -> io::Result<Option<ProcessIdentity>> {
    let contents = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(contents) => contents,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    // 进程名可能含空格与括号：以最后一个 `)` 为界，之后的字段才按空白切分。
    let invalid = || io::Error::new(io::ErrorKind::InvalidData, "invalid /proc stat");
    let closing = contents.rfind(')').ok_or_else(invalid)?;
    let fields: Vec<&str> = contents[closing + 1..].split_whitespace().collect();
    if fields.len() <= 19 {
        return Err(invalid());
    }
    Ok(Some(ProcessIdentity {
        pid,
        group: fields[2].parse().map_err(|_| invalid())?,
        session: fields[3].parse().map_err(|_| invalid())?,
        start_time: fields[19].parse().map_err(|_| invalid())?,
        zombie: fields[0] == "Z",
    }))
}

// 其他 Unix 没有枚举进程的接口：清理不到成员，首进程由各运行时回收前的 `kill` 结束。
#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
fn all_pids() -> Vec<libc::pid_t> {
    Vec::new()
}

#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
fn read_process_identity(pid: libc::pid_t) -> io::Result<Option<ProcessIdentity>> {
    let group = unsafe { libc::getpgid(pid) };
    let session = unsafe { libc::getsid(pid) };
    if group <= 0 || session <= 0 {
        return Ok(None);
    }
    Ok(Some(ProcessIdentity {
        pid,
        group,
        session,
        start_time: 0,
        zombie: false,
    }))
}

#[cfg(windows)]
struct WindowsJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for WindowsJob {}

#[cfg(windows)]
unsafe impl Sync for WindowsJob {}

#[cfg(windows)]
impl WindowsJob {
    fn assign(process: windows_sys::Win32::Foundation::HANDLE) -> anyhow::Result<Self> {
        use std::ptr::null;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let handle = unsafe { CreateJobObjectW(null(), null()) };
        if handle.is_null() {
            anyhow::bail!("failed to create terminal worker job object")
        }
        let job = WindowsJob(handle);
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            BasicLimitInformation: JOBOBJECT_BASIC_LIMIT_INFORMATION {
                LimitFlags: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                ..unsafe { std::mem::zeroed() }
            },
            ..unsafe { std::mem::zeroed() }
        };
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &mut limits as *mut _ as *mut _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } != 0;
        let assigned = configured && unsafe { AssignProcessToJobObject(job.0, process) != 0 };
        if !assigned {
            anyhow::bail!("failed to assign process to terminal worker job object")
        }
        Ok(job)
    }

    fn terminate(&mut self) -> std::io::Result<()> {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        if unsafe { TerminateJobObject(self.0, 1) } == 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}

#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}

#[cfg(all(test, any(target_os = "macos", target_os = "linux")))]
mod tests {
    use super::Containment;
    use portable_pty::{native_pty_system, CommandBuilder, PtySize};
    use std::io::Read;
    use std::process::Command;
    use std::time::{Duration, Instant};

    fn alive(pid: libc::pid_t) -> bool {
        unsafe { libc::kill(pid, 0) == 0 }
    }

    fn wait_until_gone(pid: libc::pid_t) -> bool {
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if !alive(pid) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        false
    }

    /// shell 自己退出后，它留下的忽略 HUP 的进程仍被清理；会话外的进程不受影响。普通 `&` 进程
    /// 与非交互 shell 同在前台进程组，会被 shell 退出时内核的 SIGHUP 顺带结束，测不出清理；先等
    /// 0.3 秒再退出，让 nohup 来得及忽略 HUP。
    #[test]
    fn terminate_reaps_session_members_after_the_shell_exits() {
        let mut outsider = Command::new("sleep").arg("30").spawn().expect("spawn outsider");
        let pair = native_pty_system()
            .openpty(PtySize::default())
            .expect("open pty");
        let mut command = CommandBuilder::new("/bin/sh");
        command.args([
            "-c",
            "nohup sleep 30 >/dev/null 2>&1 & a=$!; (trap '' HUP; exec sleep 30) & b=$!; \
             sleep 0.3; echo \"PIDS $a $b\"",
        ]);
        let mut child = pair.slave.spawn_command(command).expect("spawn shell");
        let mut containment = Containment::for_session(child.as_ref()).expect("containment");
        drop(pair.slave);

        let mut reader = pair.master.try_clone_reader().expect("reader");
        let mut output = String::new();
        let mut buffer = [0u8; 256];
        while !output.contains('\n') || !output.contains("PIDS") {
            let read = reader.read(&mut buffer).expect("read pty");
            assert!(read > 0, "shell closed before reporting pids: {output:?}");
            output.push_str(&String::from_utf8_lossy(&buffer[..read]));
        }
        let pids: Vec<libc::pid_t> = output
            .split("PIDS")
            .nth(1)
            .unwrap()
            .split_whitespace()
            .take(2)
            .map(|value| value.parse().expect("pid"))
            .collect();
        child.wait().expect("shell exits by itself");
        std::thread::sleep(Duration::from_millis(200));
        assert!(pids.iter().all(|pid| alive(*pid)), "jobs must outlive the shell");

        containment.terminate().expect("terminate");

        for pid in pids {
            assert!(wait_until_gone(pid), "session member {pid} survived");
        }
        assert!(alive(outsider.id() as libc::pid_t), "outsider must survive");
        let _ = outsider.kill();
        let _ = outsider.wait();
    }
}
