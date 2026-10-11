use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::backend_process::BackendLaunchMode;
use tauri::{AppHandle, Manager};

/// 后端的启动方式。运行时解析与进程创建分离，避免 supervisor 了解环境变量细节。
///
/// 开发模式与 `COSIR_BACKEND_PYTHON` 显式指定共用同一个 `Interpreter` 形态：两者的 launcher
/// 都是 Python 解释器、启动方式都是 `-m app`。`uv` 只参与解析（并顺带同步环境），不是被宿主
/// 监督的进程——见 [`resolve_uv_interpreter`] 的说明。
#[derive(Debug, Clone)]
pub enum BackendRuntime {
    Interpreter {
        launcher: PathBuf,
        backend_dir: PathBuf,
        terminal_worker: PathBuf,
    },
    FrozenExecutable {
        launcher: PathBuf,
        backend_dir: PathBuf,
        terminal_worker: PathBuf,
    },
}

impl BackendRuntime {
    pub fn backend_dir(&self) -> &Path {
        match self {
            Self::Interpreter { backend_dir, .. } | Self::FrozenExecutable { backend_dir, .. } => {
                backend_dir
            }
        }
    }

    pub fn launcher(&self) -> &Path {
        match self {
            Self::Interpreter { launcher, .. } | Self::FrozenExecutable { launcher, .. } => {
                launcher
            }
        }
    }

    pub fn launch_mode(&self) -> BackendLaunchMode {
        match self {
            Self::Interpreter { .. } => BackendLaunchMode::PythonModule,
            Self::FrozenExecutable { .. } => BackendLaunchMode::FrozenExecutable,
        }
    }

    pub fn terminal_worker(&self) -> &Path {
        match self {
            Self::Interpreter {
                terminal_worker, ..
            }
            | Self::FrozenExecutable {
                terminal_worker, ..
            } => terminal_worker,
        }
    }
}

/// 解析本地桌面应用应使用的后端运行时。
pub fn resolve_backend_runtime(
    app: &AppHandle,
    runtime_dir: &Path,
) -> Result<BackendRuntime, String> {
    if cfg!(debug_assertions) {
        let backend_dir = std::env::var_os("COSIR_BACKEND_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(default_backend_dir);
        let terminal_worker = development_terminal_worker()?;
        if let Some(launcher) = std::env::var_os("COSIR_BACKEND_PYTHON").map(PathBuf::from) {
            return Ok(BackendRuntime::Interpreter {
                launcher,
                backend_dir,
                terminal_worker,
            });
        }
        let cache_dir = runtime_dir.join("uv-cache");
        std::fs::create_dir_all(&cache_dir).map_err(|error| {
            format!(
                "无法创建项目级 uv 缓存目录：{}：{error}",
                cache_dir.display()
            )
        })?;
        let launcher = resolve_uv_interpreter(&backend_dir, &cache_dir)?;
        return Ok(BackendRuntime::Interpreter {
            launcher,
            backend_dir,
            terminal_worker,
        });
    }

    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法解析随应用交付的后端运行时目录：{error}"))?;
    let backend_dir = resource_dir.join("app-resources").join("backend");
    if !backend_dir.is_dir() {
        return Err(format!(
            "随应用交付的后端运行目录不存在：{}",
            backend_dir.display()
        ));
    }
    let launcher = packaged_backend_executable(&backend_dir);
    if !launcher.is_file() {
        return Err(format!(
            "随应用交付的本地后端可执行文件不存在：{}",
            launcher.display()
        ));
    }
    let terminal_worker = packaged_terminal_worker(&resource_dir);
    if !terminal_worker.is_file() {
        return Err(format!(
            "随应用交付的 Terminal Worker 不存在：{}",
            terminal_worker.display()
        ));
    }
    Ok(BackendRuntime::FrozenExecutable {
        launcher,
        backend_dir,
        terminal_worker,
    })
}

/// 用 `uv` 同步项目环境并解析出后端应使用的 Python 解释器路径。
///
/// 宿主刻意**不**把 `uv run` 当作被监督的 launcher：那样真正的 Python 会变成 uv 的孙进程，
/// 而退出清理只对直接子进程发 SIGKILL（无法被 uv 转发），uv 一死 Python 立刻被 launchd 收养
/// 并永久驻留——实测每次退出泄漏一个后端（11 天累积 40 个实例，各自占端口并长期持有 SQLite）。
/// 因此这里只用 uv 完成「解析 + 环境同步」这一步，之后由宿主直接 spawn 解释器：后端成为
/// 直接子进程，进程收尾与信号都真正落在它身上，同时保留 uv 依赖同步（首次运行会按
/// `apps/backend/uv.lock` 安装依赖）的保证。
///
/// 参数:
///     backend_dir: 后端项目目录，`uv` 以它作为项目根。
///     cache_dir: 项目级 uv 缓存目录，避免污染用户全局缓存。
///
/// 返回:
///     解析出的解释器绝对路径；已确认是文件。
///
/// 异常:
///     uv 不存在、命令失败或输出不是可用文件路径时返回启动错误；错误文案保留
///     `COSIR_BACKEND_PYTHON` 逃逸口，便于用户绕过 uv。
fn resolve_uv_interpreter(backend_dir: &Path, cache_dir: &Path) -> Result<PathBuf, String> {
    let output = Command::new("uv")
        .args(["run", "--directory"])
        .arg(backend_dir)
        .args(["python", "-c", "import sys; print(sys.executable)"])
        .env("UV_CACHE_DIR", cache_dir)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| {
            format!(
                "开发环境未找到 uv，请安装 uv 或显式设置 COSIR_BACKEND_PYTHON 指向 Python 解释器：{error}"
            )
        })?;
    if !output.status.success() {
        return Err(format!(
            "uv 解析后端解释器失败（退出码 {}）：{}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let launcher = PathBuf::from(String::from_utf8_lossy(&output.stdout).trim());
    if !launcher.is_file() {
        return Err(format!(
            "uv 返回的后端解释器路径不可用：{}",
            launcher.display()
        ));
    }
    Ok(launcher)
}

fn default_backend_dir() -> PathBuf {
    if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("backend")
    } else {
        PathBuf::from("backend")
    }
}

fn packaged_backend_executable(backend_dir: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        backend_dir.join("cosir-backend.exe")
    }
    #[cfg(not(windows))]
    {
        backend_dir.join("cosir-backend")
    }
}

fn terminal_worker_filename() -> &'static str {
    if cfg!(windows) {
        "terminal-worker.exe"
    } else {
        "terminal-worker"
    }
}

/// 读取开发编排器注入的 Terminal Worker 路径并确认产物存在。
///
/// 开发模式不再根据当前工作目录、crate 目录或历史 target 目录猜测路径；
/// 唯一合法来源是桌面开发编排器设置的 ``CODING_AGENT_TERMINAL_WORKER``。
/// 路径不存在时直接阻止后端启动，避免进入没有终端能力的半可用状态。
///
/// 返回：已存在的 Worker 可执行文件路径。
///
/// 异常：环境变量缺失或路径不是文件时返回启动错误，由 supervisor 呈现给桌面层。
fn development_terminal_worker() -> Result<PathBuf, String> {
    let configured = std::env::var_os("CODING_AGENT_TERMINAL_WORKER")
        .ok_or_else(|| "开发模式未注入 CODING_AGENT_TERMINAL_WORKER".to_string())?;
    let worker = PathBuf::from(configured);
    if !worker.is_file() {
        return Err(format!(
            "开发模式 Terminal Worker 不存在：{}",
            worker.display()
        ));
    }
    Ok(worker)
}

fn packaged_terminal_worker(resource_dir: &Path) -> PathBuf {
    resource_dir
        .join("app-resources")
        .join("terminal-worker")
        .join(terminal_worker_filename())
}

#[cfg(test)]
mod tests {
    use super::{packaged_backend_executable, BackendRuntime};
    use std::path::PathBuf;

    #[test]
    fn interpreter_runtime_launches_python_module_directly() {
        let runtime = BackendRuntime::Interpreter {
            launcher: PathBuf::from("backend/.venv/bin/python"),
            backend_dir: PathBuf::from("backend"),
            terminal_worker: PathBuf::from("terminal-worker"),
        };
        assert_eq!(
            runtime.launch_mode(),
            crate::backend_process::BackendLaunchMode::PythonModule
        );
        assert_eq!(
            runtime.launcher(),
            std::path::Path::new("backend/.venv/bin/python")
        );
        assert_eq!(runtime.backend_dir(), std::path::Path::new("backend"));
    }

    #[test]
    fn packaged_backend_executable_has_platform_specific_name() {
        let path = packaged_backend_executable(std::path::Path::new("resources/backend"));
        assert!(path.ends_with(if cfg!(windows) {
            "cosir-backend.exe"
        } else {
            "cosir-backend"
        }));
    }

    #[test]
    fn frozen_runtime_uses_standalone_executable_and_user_data_directory() {
        let runtime = BackendRuntime::FrozenExecutable {
            launcher: PathBuf::from("resources/backend/cosir-backend.exe"),
            backend_dir: PathBuf::from("resources/backend"),
            terminal_worker: PathBuf::from("resources/terminal-worker/terminal-worker"),
        };
        assert_eq!(
            runtime.launch_mode(),
            crate::backend_process::BackendLaunchMode::FrozenExecutable
        );
    }
}
