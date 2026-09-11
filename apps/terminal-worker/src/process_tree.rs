#[cfg(unix)]
use std::io;

use portable_pty::{Child, MasterPty};

#[cfg(unix)]
use std::time::Duration;

#[cfg(unix)]
pub struct ProcessTreeGuard {
    process_group: Option<libc::pid_t>,
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
            let _ = child;
            Ok(Self {
                process_group: master.process_group_leader(),
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

    pub fn terminate(&mut self, child: &mut dyn Child) -> anyhow::Result<()> {
        #[cfg(unix)]
        {
            if let Some(group) = self.process_group {
                send_group_signal(group, libc::SIGTERM)?;
                for _ in 0..20 {
                    if child.try_wait()?.is_some() {
                        return Ok(());
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                let _ = send_group_signal(group, libc::SIGKILL);
            } else {
                let _ = child.kill();
            }
            let _ = child.wait();
            Ok(())
        }

        #[cfg(windows)]
        {
            if let Some(job) = self.job.take() {
                job.terminate();
            } else {
                let _ = child.kill();
            }
            let _ = child.wait();
            Ok(())
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
fn send_group_signal(group: libc::pid_t, signal: libc::c_int) -> io::Result<()> {
    let result = unsafe { libc::kill(-group, signal) };
    if result == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
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
                .map(|child_handle| unsafe { AssignProcessToJobObject(handle, child_handle) != 0 })
                .unwrap_or(false);
        if !assigned {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            anyhow::bail!("failed to assign terminal shell to job object")
        }
        Ok(Self(handle))
    }

    fn terminate(self) {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        unsafe {
            let _ = TerminateJobObject(self.0, 1);
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}
