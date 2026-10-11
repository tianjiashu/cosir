use std::ffi::OsString;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::thread;

use crate::desktop_log::append_console_line;

/// 后端进程的启动形态；launcher 由 `BackendRuntime` 解析后传入。
///
/// 刻意**没有**「`uv run …`」形态：把 uv 当作被监督的 launcher，真正的后端会变成 uv 的孙进程，
/// 而进程收尾只对直接子进程发 SIGKILL（无法被 uv 转发），uv 一死后端立刻被 launchd 收养并
/// 永久驻留（实测每次退出泄漏一个实例）。uv 只参与 `resolve_uv_interpreter` 的环境解析与同步。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendLaunchMode {
    /// 解释器 + `-m app`：开发模式（解释器由 uv 解析）与显式指定解释器共用。
    PythonModule,
    /// 随应用交付的自包含后端可执行文件。
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
    pub data_dir: Option<&'a Path>,
    pub terminal_worker: &'a Path,
    pub log_file: &'a Path,
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
        BackendLaunchMode::PythonModule => {
            command.args(["-m", "app"]);
            for (key, value) in interpreter_environment(config.launcher) {
                command.env(key, value);
            }
        }
        BackendLaunchMode::FrozenExecutable => {}
    }
    command.env("CODING_AGENT_TERMINAL_WORKER", config.terminal_worker);
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

/// 复现 `uv run` 曾注入的虚拟环境语义。
///
/// `uv run` 会把虚拟环境的 `bin`（Windows 为 `Scripts`）前置到 `PATH` 并设置 `VIRTUAL_ENV`；
/// 宿主改为直接托管解释器后必须自己补上，否则 Agent 在终端里执行裸 `python` / `pytest`
/// 会悄悄落到系统解释器。`VIRTUAL_ENV` 只在解释器确实位于虚拟环境（同级存在 `pyvenv.cfg`）
/// 时才设置，避免为用户显式指定的独立解释器伪造环境标记。
///
/// 参数:
///     launcher: 后端解释器路径。
///
/// 返回:
///     需要注入子进程的 `(变量名, 变量值)` 列表；无可用信息时为空。
fn interpreter_environment(launcher: &Path) -> Vec<(OsString, OsString)> {
    let mut values = Vec::new();
    let Some(bin_dir) = launcher.parent() else {
        return values;
    };
    if let Some(path) = std::env::var_os("PATH") {
        let mut entries = vec![bin_dir.to_path_buf()];
        entries.extend(std::env::split_paths(&path));
        if let Ok(joined) = std::env::join_paths(entries) {
            values.push((OsString::from("PATH"), joined));
        }
    }
    if let Some(root) = bin_dir.parent() {
        if root.join("pyvenv.cfg").is_file() {
            values.push((
                OsString::from("VIRTUAL_ENV"),
                root.as_os_str().to_os_string(),
            ));
        }
    }
    values
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

#[cfg(test)]
mod tests {
    use super::interpreter_environment;
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    fn unique_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("cosir-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("创建临时目录");
        dir
    }

    fn binary_name() -> &'static str {
        if cfg!(windows) {
            "python.exe"
        } else {
            "python"
        }
    }

    fn value_of(values: &[(OsString, OsString)], key: &str) -> Option<OsString> {
        values
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.clone())
    }

    #[test]
    fn interpreter_environment_prepends_venv_bin_and_sets_virtual_env() {
        let root = unique_dir("venv");
        let bin_dir = root.join(if cfg!(windows) { "Scripts" } else { "bin" });
        std::fs::create_dir_all(&bin_dir).expect("创建虚拟环境 bin 目录");
        std::fs::write(root.join("pyvenv.cfg"), "home = /usr/bin\n").expect("写入 pyvenv.cfg");

        let values = interpreter_environment(&bin_dir.join(binary_name()));
        let path = value_of(&values, "PATH").expect("PATH 应被注入");
        assert_eq!(
            std::env::split_paths(&path).next().as_deref(),
            Some(bin_dir.as_path())
        );
        let virtual_env = value_of(&values, "VIRTUAL_ENV").expect("虚拟环境应注入 VIRTUAL_ENV");
        assert_eq!(Path::new(&virtual_env), root.as_path());

        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn interpreter_environment_skips_virtual_env_for_standalone_python() {
        let dir = unique_dir("plain-python");
        let values = interpreter_environment(&dir.join(binary_name()));

        assert!(value_of(&values, "VIRTUAL_ENV").is_none());
        assert!(value_of(&values, "PATH").is_some());

        let _ = std::fs::remove_dir_all(&dir);
    }
}
