//! 本地后端运行时定位器。

use crate::backend::types::BackendLaunchConfig;
use std::path::{Path, PathBuf};

/// 解析本地后端运行所需的路径与固定参数。
///
/// 参数:
///     _app: Tauri 应用句柄；当前阶段仅用于保持接口稳定。
///
/// 返回:
///     运行后端所需的完整配置。
///
/// 异常:
///     当仓库根目录、后端目录或 Python 运行时缺失时返回错误字符串。
///
/// 副作用:
///     无。
pub fn resolve_backend_launch_config(_app: &tauri::AppHandle) -> Result<BackendLaunchConfig, String> {
    let repo_root = resolve_repo_root()?;
    let backend_dir = repo_root.join("apps/backend");
    if !backend_dir.exists() {
        return Err(format!(
            "未找到后端目录: {}",
            backend_dir.display()
        ));
    }

    let python_binary = resolve_python_binary(&backend_dir)?;
    let log_dir = repo_root.join("logs");

    Ok(BackendLaunchConfig {
        repo_root: repo_root.clone(),
        backend_dir,
        python_binary,
        host: "127.0.0.1".to_string(),
        port: 8000,
        app_log_file: log_dir.join("app.log"),
        stdout_log_file: log_dir.join("backend-stdout.log"),
        stderr_log_file: log_dir.join("backend-stderr.log"),
    })
}

/// 解析仓库根目录。
///
/// 参数:
///     无。
///
/// 返回:
///     仓库根目录的规范化绝对路径。
///
/// 异常:
///     当固定相对路径不再成立或无法规范化时返回错误字符串。
///
/// 副作用:
///     无。
fn resolve_repo_root() -> Result<PathBuf, String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir.join("../../..");
    repo_root
        .canonicalize()
        .map_err(|e| format!("无法解析仓库根目录 {}: {e}", repo_root.display()))
}

/// 解析项目内 Python 运行时路径。
///
/// 参数:
///     backend_dir: `apps/backend` 目录。
///
/// 返回:
///     可执行 Python 路径。
///
/// 异常:
///     当 `.venv` 中未找到可执行 Python 时返回错误字符串。
///
/// 副作用:
///     无。
fn resolve_python_binary(backend_dir: &Path) -> Result<PathBuf, String> {
    #[cfg(target_os = "windows")]
    let candidates = [backend_dir.join(".venv").join("Scripts").join("python.exe")];

    #[cfg(not(target_os = "windows"))]
    let candidates = [
        backend_dir.join(".venv").join("bin").join("python"),
        backend_dir.join(".venv").join("bin").join("python3"),
    ];

    for candidate in candidates {
        if candidate.exists() {
            return Ok(candidate);
        }
    }

    Err(format!(
        "未找到项目内 Python 运行时，请先创建 apps/backend/.venv。查找位置: {}",
        backend_dir.join(".venv").display()
    ))
}
