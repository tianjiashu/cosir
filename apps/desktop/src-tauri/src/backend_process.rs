use std::path::Path;
use std::process::{Child, Command, Stdio};

pub struct BackendProcess {
    pub child: Child,
    #[cfg(windows)]
    _job: WindowsJob,
}

#[cfg(windows)]
struct WindowsJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for WindowsJob {}

#[cfg(windows)]
unsafe impl Sync for WindowsJob {}

#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}

pub fn spawn_backend(
    launcher: &Path,
    backend_dir: &Path,
    port: u16,
    bootstate_file: &Path,
    use_uv: bool,
    log_file: &Path,
) -> Result<BackendProcess, String> {
    let mut command = Command::new(launcher);
    command.current_dir(backend_dir);
    if use_uv {
        command.args(["run", "--directory"]);
        command.arg(backend_dir);
        command.args(["python", "-m", "app"]);
    } else {
        command.args(["-m", "app"]);
    }
    let stdout = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_file)
        .map_err(|error| format!("无法打开后端日志文件：{error}"))?;
    let stderr = stdout
        .try_clone()
        .map_err(|error| format!("无法复制后端日志句柄：{error}"))?;
    let mut child = command
        .env("CODING_AGENT_HOST", "127.0.0.1")
        .env("CODING_AGENT_PORT", port.to_string())
        .env("CODING_AGENT_RELOAD", "false")
        .env("CODING_AGENT_BOOT_STATE_FILE", bootstate_file)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .map_err(|error| format!("无法启动本地 Agent 后端：{error}"))?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if job.is_null() {
            terminate_child_tree(&mut child);
            return Err("无法创建后端进程作业对象".to_string());
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
                job,
                JobObjectExtendedLimitInformation,
                &mut limits as *mut _ as *mut _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } != 0;
        let assigned = configured
            && unsafe {
                AssignProcessToJobObject(
                    job,
                    child.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE,
                )
            } != 0;
        if !assigned {
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(job);
            }
            terminate_child_tree(&mut child);
            return Err("无法将后端进程加入作业对象".to_string());
        }
        return Ok(BackendProcess {
            child,
            _job: WindowsJob(job),
        });
    }
    #[cfg(not(windows))]
    Ok(BackendProcess { child })
}

pub fn terminate_process_tree(process: &mut BackendProcess) {
    terminate_child_tree(&mut process.child);
}

fn terminate_child_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        let pid = child.id().to_string();
        let _ = Command::new("taskkill").args(["/PID", &pid, "/T"]).status();
        std::thread::sleep(std::time::Duration::from_secs(2));
        if child.try_wait().ok().flatten().is_none() {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid, "/T", "/F"])
                .status();
        }
    }
    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}
