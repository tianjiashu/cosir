//! 本地后端进程启动器。

use crate::backend::types::BackendLaunchConfig;
use chrono::Utc;
use std::fs::{create_dir_all, OpenOptions};
use std::process::{Child, Command, Stdio};

/// 已拉起的后端进程信息。
#[derive(Debug)]
pub struct LaunchedBackendProcess {
    /// Python 子进程句柄。
    pub child: Child,
    /// 进程启动时间。
    pub started_at: String,
}

/// 启动本地 Python 后端。
///
/// 参数:
///     config: 运行时定位器解析出的启动配置。
///
/// 返回:
///     子进程句柄和启动时间。
///
/// 异常:
///     当日志文件无法打开或 Python 进程无法拉起时返回错误字符串。
///
/// 副作用:
///     创建日志目录并生成新的 Python 子进程。
pub fn launch_backend_process(
    config: &BackendLaunchConfig,
) -> Result<LaunchedBackendProcess, String> {
    if let Some(parent) = config.stdout_log_file.parent() {
        create_dir_all(parent)
            .map_err(|e| format!("无法创建后端日志目录 {}: {e}", parent.display()))?;
    }

    let stdout_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&config.stdout_log_file)
        .map_err(|e| {
            format!(
                "无法打开后端 stdout 日志文件 {}: {e}",
                config.stdout_log_file.display()
            )
        })?;
    let stderr_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&config.stderr_log_file)
        .map_err(|e| {
            format!(
                "无法打开后端 stderr 日志文件 {}: {e}",
                config.stderr_log_file.display()
            )
        })?;

    let child = Command::new(&config.python_binary)
        .arg("-m")
        .arg("app")
        .current_dir(&config.backend_dir)
        .env("CODING_AGENT_HOST", &config.host)
        .env("CODING_AGENT_PORT", config.port.to_string())
        .env("CODING_AGENT_RELOAD", "false")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| {
            format!(
                "无法启动本地 Python 后端 {}: {e}",
                config.python_binary.display()
            )
        })?;

    Ok(LaunchedBackendProcess {
        child,
        started_at: Utc::now().to_rfc3339(),
    })
}
