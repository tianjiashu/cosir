use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::thread;

use crate::desktop_log::append_console_line;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendLaunchMode {
    UvProject,
    PythonModule,
    FrozenExecutable,
}

pub struct BackendProcess {
    pub child: Child,
    #[cfg(windows)]
    _job: WindowsJob,
}

pub struct BackendLaunchConfig<'a> {
    pub launcher: &'a Path,
    pub backend_dir: &'a Path,
    pub port: u16,
    pub bootstate_file: &'a Path,
    pub launch_mode: BackendLaunchMode,
    pub uv_cache_dir: Option<&'a Path>,
    pub data_dir: Option<&'a Path>,
    pub terminal_worker: Option<&'a Path>,
    pub log_file: &'a Path,
    pub structured_log_dir: &'a Path,
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

pub fn spawn_backend(config: &BackendLaunchConfig<'_>) -> Result<BackendProcess, String> {
    let mut command = Command::new(config.launcher);
    command.current_dir(config.backend_dir);
    match config.launch_mode {
        BackendLaunchMode::UvProject => {
            command.args(["run", "--directory"]);
            command.arg(config.backend_dir);
            command.args(["python", "-m", "app"]);
        }
        BackendLaunchMode::PythonModule => {
            command.args(["-m", "app"]);
        }
        BackendLaunchMode::FrozenExecutable => {}
    }
    if let Some(cache_dir) = config.uv_cache_dir {
        command.env("UV_CACHE_DIR", cache_dir);
    }
    if let Some(worker) = config.terminal_worker {
        command.env("CODING_AGENT_TERMINAL_WORKER", worker);
    }
    if let Some(data_dir) = config.data_dir {
        command.env("CODING_AGENT_DATA_DIR", data_dir);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;

        // Keep development launches quiet too; the frozen backend is built with
        // PyInstaller's --windowed mode, while uv/python still need this flag.
        command.creation_flags(0x08000000);
    }
    let mut child = command
        .env("CODING_AGENT_PORT", config.port.to_string())
        .env("CODING_AGENT_RELOAD", "false")
        .env("CODING_AGENT_BOOT_STATE_FILE", config.bootstate_file)
        .env("CODING_AGENT_LOG_DIR", config.structured_log_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动本地 Agent 后端：{error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法接管后端 stdout".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "无法接管后端 stderr".to_string())?;
    spawn_output_forwarder(stdout, config.log_file, "stdout");
    spawn_output_forwarder(stderr, config.log_file, "stderr");
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
        Ok(BackendProcess {
            child,
            _job: WindowsJob(job),
        })
    }
    #[cfg(not(windows))]
    Ok(BackendProcess { child })
}

fn spawn_output_forwarder<R>(reader: R, log_file: &Path, stream: &'static str)
where
    R: std::io::Read + Send + 'static,
{
    let path = log_file.to_path_buf();
    thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            let _ = append_console_line(&path, stream, &line);
        }
    });
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
