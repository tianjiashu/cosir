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
        terminal_worker: Option<PathBuf>,
    },
    Interpreter {
        launcher: PathBuf,
        backend_dir: PathBuf,
        terminal_worker: Option<PathBuf>,
    },
    FrozenExecutable {
        launcher: PathBuf,
        backend_dir: PathBuf,
        terminal_worker: Option<PathBuf>,
    },
}

impl BackendRuntime {
    pub fn backend_dir(&self) -> &Path {
        match self {
            Self::UvProject { backend_dir, .. }
            | Self::Interpreter { backend_dir, .. }
            | Self::FrozenExecutable { backend_dir, .. } => {
                backend_dir
            }
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

    pub fn uses_packaged_data_dir(&self) -> bool {
        matches!(self, Self::FrozenExecutable { .. })
    }

    pub fn uv_cache_dir(&self) -> Option<&Path> {
        match self {
            Self::UvProject { cache_dir, .. } => Some(cache_dir),
            Self::Interpreter { .. } | Self::FrozenExecutable { .. } => None,
        }
    }

    pub fn terminal_worker(&self) -> Option<&Path> {
        match self {
            Self::UvProject {
                terminal_worker, ..
            }
            | Self::Interpreter {
                terminal_worker, ..
            }
            | Self::FrozenExecutable {
                terminal_worker, ..
            } => terminal_worker.as_deref(),
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
        if let Some(launcher) = std::env::var_os("COSIR_BACKEND_PYTHON").map(PathBuf::from) {
            return Ok(BackendRuntime::Interpreter {
                launcher,
                backend_dir,
                terminal_worker: development_terminal_worker(),
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
            terminal_worker: development_terminal_worker(),
        });
    }

    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法解析随应用交付的后端运行时目录：{error}"))?;
    let backend_dir = resource_dir.join("backend");
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
        terminal_worker: Some(terminal_worker),
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

/// 解析开发模式下可用的 Terminal Worker 可执行文件。
///
/// 优先使用 `CODING_AGENT_TERMINAL_WORKER` 环境变量；否则回退到仓库统一的
/// Cargo 构建目录 `target/debug`（由仓库根 `.cargo/config.toml` 的
/// `target-dir` 指定），而不是各 crate 目录下的 `apps/terminal-worker/target`。
///
/// 候选文件不存在时返回 `None`，由调用方按“无 Terminal Worker”降级处理，
/// 不会中断后端启动。
fn development_terminal_worker() -> Option<PathBuf> {
    if let Some(configured) = std::env::var_os("CODING_AGENT_TERMINAL_WORKER") {
        return Some(PathBuf::from(configured));
    }
    let candidate = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("target")
        .join("debug")
        .join(terminal_worker_filename());
    candidate.is_file().then_some(candidate)
}

fn packaged_terminal_worker(resource_dir: &Path) -> PathBuf {
    resource_dir
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
            terminal_worker: None,
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
            terminal_worker: None,
        };
        assert_eq!(
            runtime.launch_mode(),
            crate::backend_process::BackendLaunchMode::FrozenExecutable
        );
        assert!(runtime.uses_packaged_data_dir());
        assert_eq!(runtime.uv_cache_dir(), None);
    }
}
