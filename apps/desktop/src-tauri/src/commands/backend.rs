//! 本地后端托管命令。

use crate::backend::supervisor::BackendSupervisorState;
use crate::backend::types::{BackendLogsTailResponse, BackendStatusResponse};
use tauri::State;

/// 启动本地 Python 后端。
///
/// 参数:
///     app: Tauri 应用句柄。
///     supervisor: 后端托管状态。
///
/// 返回:
///     启动后的结构化后端状态。
///
/// 异常:
///     仅当 supervisor 锁中毒等不可恢复错误发生时返回错误字符串。
///
/// 副作用:
///     可能拉起本地 Python 子进程并触发健康检查轮询。
#[tauri::command]
pub fn backend_start(
    app: tauri::AppHandle,
    supervisor: State<'_, BackendSupervisorState>,
) -> Result<BackendStatusResponse, String> {
    supervisor.start(&app)
}

/// 停止当前由桌面端托管的本地 Python 后端。
///
/// 参数:
///     app: Tauri 应用句柄。
///     supervisor: 后端托管状态。
///
/// 返回:
///     停止后的结构化后端状态。
///
/// 异常:
///     仅当 supervisor 锁中毒等不可恢复错误发生时返回错误字符串。
///
/// 副作用:
///     可能终止已托管的 Python 子进程。
#[tauri::command]
pub fn backend_stop(
    app: tauri::AppHandle,
    supervisor: State<'_, BackendSupervisorState>,
) -> Result<BackendStatusResponse, String> {
    supervisor.stop(&app)
}

/// 重启当前由桌面端托管的本地 Python 后端。
///
/// 参数:
///     app: Tauri 应用句柄。
///     supervisor: 后端托管状态。
///
/// 返回:
///     重启后的结构化后端状态。
///
/// 异常:
///     仅当 supervisor 锁中毒等不可恢复错误发生时返回错误字符串。
///
/// 副作用:
///     可能终止旧的 Python 子进程并拉起新进程。
#[tauri::command]
pub fn backend_restart(
    app: tauri::AppHandle,
    supervisor: State<'_, BackendSupervisorState>,
) -> Result<BackendStatusResponse, String> {
    supervisor.restart(&app)
}

/// 查询本地后端当前状态。
///
/// 参数:
///     app: Tauri 应用句柄。
///     supervisor: 后端托管状态。
///
/// 返回:
///     当前结构化后端状态。
///
/// 异常:
///     仅当 supervisor 锁中毒等不可恢复错误发生时返回错误字符串。
///
/// 副作用:
///     会探测现有进程和 `/health` 端点。
#[tauri::command]
pub fn backend_status(
    app: tauri::AppHandle,
    supervisor: State<'_, BackendSupervisorState>,
) -> Result<BackendStatusResponse, String> {
    supervisor.status(&app)
}

/// 读取后端相关日志的尾部内容。
///
/// 参数:
///     app: Tauri 应用句柄。
///     supervisor: 后端托管状态。
///     max_lines: 每个日志文件最多返回的尾部行数。
///
/// 返回:
///     每个日志文件的尾部文本。
///
/// 异常:
///     仅当 supervisor 锁中毒等不可恢复错误发生时返回错误字符串。
///
/// 副作用:
///     读取本地日志文件。
#[tauri::command]
pub fn backend_logs_tail(
    app: tauri::AppHandle,
    supervisor: State<'_, BackendSupervisorState>,
    max_lines: Option<usize>,
) -> Result<BackendLogsTailResponse, String> {
    supervisor.logs_tail(&app, max_lines.unwrap_or(80))
}
