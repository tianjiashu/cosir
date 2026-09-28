use std::path::{Path, PathBuf};

use crate::backend_process::BackendLaunchMode;
use tauri::{AppHandle, Manager};

/// 后端的启动方式。运行时解析与进程创建分离，避免 supervisor 了解环境变量细节。
#[derive(Debug, Clone)]
pub enum BackendRuntime {
    UvProject {
        launcher: PathBuf,
        backend_dir: PathBuf,
        cache_dir: PathBuf,
        terminal_worker: PathBuf,
    },
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
            Self::UvProject { backend_dir, .. }
            | Self::Interpreter { backend_dir, .. }
            | Self::FrozenExecutable { backend_dir, .. } => backend_dir,
        }
    }

    pub fn launcher(&self) -> &Path {
        match self {
            Self::UvProject { launcher, .. }
            | Self::Interpreter { launcher, .. }
            | Self::FrozenExecutable { launcher, .. } => launcher,
        }
    }

    pub fn launch_mode(&self) -> BackendLaunchMode {
        match self {
            Self::UvProject { .. } => BackendLaunchMode::UvProject,
            Self::Interpreter { .. } => BackendLaunchMode::PythonModule,
            Self::FrozenExecutable { .. } => BackendLaunchMode::FrozenExecutable,
        }
    }

    pub fn uv_cache_dir(&self) -> Option<&Path> {
        match self {
            Self::UvProject { cache_dir, .. } => Some(cache_dir),
            Self::Interpreter { .. } | Self::FrozenExecutable { .. } => None,
        }
    }

    pub fn terminal_worker(&self) -> &Path {
        match self {
            Self::UvProject {
                terminal_worker, ..
            }
            | Self::Interpreter {
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
        if std::process::Command::new("uv")
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_err()
        {
            return Err(
                "开发环境未找到 uv，请安装 uv 或显式设置 COSIR_BACKEND_PYTHON 指向 Python 解释器"
                    .to_string(),
            );
        }
        let cache_dir = runtime_dir.join("uv-cache");
        std::fs::create_dir_all(&cache_dir).map_err(|error| {
            format!(
                "无法创建项目级 uv 缓存目录：{}：{error}",
                cache_dir.display()
            )
        })?;
        return Ok(BackendRuntime::UvProject {
            launcher: PathBuf::from("uv"),
            backend_dir,
            cache_dir,
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
    fn runtime_properties_keep_uv_details_inside_uv_variant() {
        let runtime = BackendRuntime::UvProject {
            launcher: PathBuf::from("uv"),
            backend_dir: PathBuf::from("backend"),
            cache_dir: PathBuf::from("runtime/uv-cache"),
            terminal_worker: PathBuf::from("terminal-worker"),
        };
        assert_eq!(
            runtime.uv_cache_dir(),
            Some(std::path::Path::new("runtime/uv-cache"))
        );
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
        assert_eq!(runtime.uv_cache_dir(), None);
    }
}
