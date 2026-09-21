#[cfg(unix)]
use std::cmp::Reverse;
#[cfg(unix)]
use std::collections::{BTreeMap, BTreeSet};
#[cfg(unix)]
use std::io;

use portable_pty::{Child, MasterPty};

#[cfg(unix)]
use std::time::{Duration, Instant};

#[cfg(unix)]
pub struct ProcessTreeGuard {
    containment: ProcessContainment,
}

#[cfg(windows)]
pub struct ProcessTreeGuard {
    job: Option<WindowsJob>,
}

#[cfg(not(any(unix, windows)))]
pub struct ProcessTreeGuard;

impl ProcessTreeGuard {
    pub fn attach(master: &dyn MasterPty, child: &dyn Child) -> anyhow::Result<Self> {
        #[cfg(unix)]
        {
            let _ = master;
            let shell_pid: libc::pid_t = child
                .process_id()
                .and_then(|pid| pid.try_into().ok())
                .ok_or_else(|| anyhow::anyhow!("terminal shell pid unavailable"))?;
            let shell = read_process_identity(shell_pid)?
                .ok_or_else(|| anyhow::anyhow!("terminal shell process identity unavailable"))?;
            Ok(Self {
                containment: ProcessContainment {
                    shell,
                    observed: BTreeMap::from([(shell.pid, shell)]),
                },
            })
        }

        #[cfg(windows)]
        {
            let _ = master;
            Ok(Self {
                job: Some(WindowsJob::assign(child)?),
            })
        }

        #[cfg(not(any(unix, windows)))]
        {
            let _ = (master, child);
            Ok(Self)
        }
    }

    pub fn terminate(
        &mut self,
        child: &mut dyn Child,
        master: &dyn MasterPty,
    ) -> anyhow::Result<()> {
        #[cfg(unix)]
        {
            let _ = master;
            self.containment.refresh_descendants();
            let grace_deadline = Instant::now() + Duration::from_secs(1);
            let mut signal_failures = 0;
            while Instant::now() < grace_deadline {
                if let Err(error) = child.try_wait() {
                    if !process_gone(&error) {
                        signal_failures += 1;
                    }
                }
                self.containment.refresh_descendants();
                signal_failures += self.containment.signal_targets(libc::SIGTERM);
                std::thread::sleep(Duration::from_millis(50));
            }
            self.containment.refresh_descendants();
            signal_failures += self.containment.signal_targets(libc::SIGKILL);
            if let Err(error) = child.kill() {
                if !process_gone(&error) {
                    signal_failures += 1;
                }
            }
            child.wait().map(|_| ()).map_err(anyhow::Error::from)?;
            if signal_failures > 0 {
                anyhow::bail!("failed to signal {signal_failures} trusted terminal processes")
            }
            Ok(())
        }

        #[cfg(windows)]
        {
            let _ = master;
            let terminate_result = self
                .job
                .take()
                .map(|mut job| job.terminate())
                .unwrap_or_else(|| child.kill());
            let wait_result = child.wait().map(|_| ()).map_err(Into::into);
            if let Err(error) = terminate_result {
                let _ = child.kill();
                return wait_result.and_then(|_| Err(error.into()));
            }
            wait_result
        }

        #[cfg(not(any(unix, windows)))]
        {
            let _ = child.kill();
            let _ = child.wait();
            Ok(())
        }
    }
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
struct ProcessIdentity {
    pid: libc::pid_t,
    parent: libc::pid_t,
    group: libc::pid_t,
    session: libc::pid_t,
    start_time: u64,
}

#[cfg(unix)]
struct ProcessContainment {
    shell: ProcessIdentity,
    observed: BTreeMap<libc::pid_t, ProcessIdentity>,
}

#[cfg(unix)]
impl ProcessContainment {
    fn refresh_descendants(&mut self) {
        if !self.shell_identity_is_current() {
            return;
        }
        let mut pending = vec![self.shell.pid];
        let mut visited = BTreeSet::new();
        while let Some(parent) = pending.pop() {
            if !visited.insert(parent) {
                continue;
            }
            for child in child_processes(parent) {
                if let Ok(Some(identity)) = read_process_identity(child) {
                    if identity.parent > 0 && identity.parent != parent {
                        continue;
                    }
                    self.observed.insert(identity.pid, identity);
                    pending.push(identity.pid);
                }
            }
        }
    }

    fn shell_identity_is_current(&self) -> bool {
        matches!(
            read_process_identity(self.shell.pid),
            Ok(Some(current)) if current.start_time == self.shell.start_time
        )
    }

    fn signal_targets(&mut self, signal: libc::c_int) -> usize {
        let mut failures = 0;
        let mut identities: Vec<_> = self.observed.values().copied().collect();
        identities.sort_by_key(|identity| Reverse(self.depth(identity.pid)));
        for identity in identities {
            match read_process_identity(identity.pid) {
                Ok(Some(current)) => {
                    if current.pid != identity.pid
                        || current.start_time != identity.start_time
                        || current.group <= 0
                        || current.session <= 0
                        || current.session != self.shell.session
                    {
                        continue;
                    }
                    if let Err(error) = send_process_signal(identity.pid, signal) {
                        if error.raw_os_error() != Some(libc::ESRCH) {
                            failures += 1;
                        }
                    }
                }
                Ok(None) => {}
                Err(_) => failures += 1,
            }
        }
        failures
    }

    fn depth(&self, pid: libc::pid_t) -> usize {
        let mut current = pid;
        let mut depth = 0;
        let mut visited = BTreeSet::new();
        while visited.insert(current) {
            let Some(identity) = self.observed.get(&current) else {
                break;
            };
            let Some(parent) = self.observed.get(&identity.parent) else {
                break;
            };
            current = parent.pid;
            depth += 1;
        }
        depth
    }
}

#[cfg(unix)]
fn send_process_signal(pid: libc::pid_t, signal: libc::c_int) -> io::Result<()> {
    if unsafe { libc::kill(pid, signal) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn process_gone(error: &io::Error) -> bool {
    matches!(
        error.raw_os_error(),
        Some(code) if code == libc::ESRCH || code == libc::ECHILD
    )
}

#[cfg(target_os = "linux")]
fn read_process_identity(pid: libc::pid_t) -> io::Result<Option<ProcessIdentity>> {
    let path = format!("/proc/{pid}/stat");
    let contents = match std::fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let closing = contents
        .rfind(')')
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid /proc stat"))?;
    let fields: Vec<&str> = contents[closing + 1..].split_whitespace().collect();
    if fields.len() <= 19 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "short /proc stat",
        ));
    }
    Ok(Some(ProcessIdentity {
        pid,
        parent: fields[1].parse().map_err(invalid_process_stat)?,
        group: fields[2].parse().map_err(invalid_process_stat)?,
        session: fields[3].parse().map_err(invalid_process_stat)?,
        start_time: fields[19].parse().map_err(invalid_process_stat)?,
    }))
}

#[cfg(target_os = "linux")]
fn child_processes(pid: libc::pid_t) -> Vec<libc::pid_t> {
    let path = format!("/proc/{pid}/task/{pid}/children");
    std::fs::read_to_string(path)
        .ok()
        .into_iter()
        .flat_map(|contents| {
            contents
                .split_whitespace()
                .filter_map(|value| value.parse().ok())
                .collect::<Vec<libc::pid_t>>()
        })
        .collect()
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
        parent: info.pbi_ppid as libc::pid_t,
        group: info.pbi_pgid as libc::pid_t,
        session,
        start_time: info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec,
    }))
}

#[cfg(target_os = "macos")]
fn child_processes(pid: libc::pid_t) -> Vec<libc::pid_t> {
    const MAX_PID_BUFFER: usize = 1 << 20;
    let mut capacity = 128;
    loop {
        let mut children = vec![0 as libc::pid_t; capacity];
        let size = unsafe {
            libc::proc_listchildpids(
                pid,
                children.as_mut_ptr().cast(),
                (children.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int,
            )
        };
        if size <= 0 {
            return Vec::new();
        }
        // proc_listchildpids returns the number of PIDs, not the number of
        // bytes written.  Treating this value as bytes makes every small
        // result truncate to zero on macOS and loses the whole descendant
        // tree during cleanup.
        let count = (size as usize).min(children.len());
        if count < children.len() {
            children.truncate(count);
            return children.into_iter().filter(|pid| *pid > 0).collect();
        }
        if children.len() * std::mem::size_of::<libc::pid_t>() >= MAX_PID_BUFFER {
            eprintln!("terminal child scan truncated for pid {pid}");
            children.truncate(count);
            return children.into_iter().filter(|pid| *pid > 0).collect();
        }
        capacity *= 2;
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::child_processes;
    use std::process::Command;

    #[test]
    fn discovers_a_direct_child_on_macos() {
        let mut child = Command::new("sleep").arg("2").spawn().expect("spawn child");
        let pid = child.id() as libc::pid_t;

        assert!(child_processes(std::process::id() as libc::pid_t).contains(&pid));

        let _ = child.kill();
        let _ = child.wait();
    }
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
        parent: 0,
        group,
        session,
        start_time: 0,
    }))
}

#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
fn child_processes(_pid: libc::pid_t) -> Vec<libc::pid_t> {
    Vec::new()
}

#[cfg(target_os = "linux")]
fn invalid_process_stat<T>(_error: T) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid /proc stat field")
}

#[cfg(windows)]
struct WindowsJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for WindowsJob {}

#[cfg(windows)]
unsafe impl Sync for WindowsJob {}

#[cfg(windows)]
impl WindowsJob {
    fn assign(child: &dyn Child) -> anyhow::Result<Self> {
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
        let assigned = configured
            && child
                .as_raw_handle()
                .map(|child_handle| unsafe { AssignProcessToJobObject(job.0, child_handle) != 0 })
                .unwrap_or(false);
        if !assigned {
            anyhow::bail!("failed to assign terminal shell to job object")
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
