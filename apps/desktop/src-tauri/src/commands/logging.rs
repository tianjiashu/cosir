//! 前端日志落盘命令。

use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use tauri::Manager;

/// 日志条目结构（从前端传入）。
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    /// 日志级别 (debug/info/warn/error)。
    pub level: String,
    /// 日志消息文本。
    pub message: String,
    /// ISO-8601 时间戳。
    pub timestamp: String,
    /// 可选的附加上下文 JSON 字符串。
    #[serde(default)]
    pub context: Option<serde_json::Value>,
    /// 可选的错误堆栈。
    #[serde(default)]
    pub stack: Option<String>,
}

/// 将前端日志条目追加写入 `desktop.log`。
///
/// 参数:
///     app: Tauri 应用句柄，用于解析平台日志目录。
///     entry: 由前端传入的结构化日志条目。
///
/// 返回:
///     成功时返回 `Ok(())`。
///
/// 异常:
///     当日志目录解析、文件打开或写入失败时，返回错误字符串。
///
/// 副作用:
///     创建日志目录并向 `desktop.log` 追加写入一条或多条日志。
#[tauri::command]
pub fn log_write(app: tauri::AppHandle, entry: LogEntry) -> Result<(), String> {
    let app_dir = get_log_dir(&app)?;
    let log_path = app_dir.join("desktop.log");

    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("无法创建日志目录: {e}"))?;
    }

    let message = if entry.message.len() > 8192 {
        format!("{}…(已截断)", &entry.message[..8192])
    } else {
        entry.message.clone()
    };

    let line = format!(
        "[{}] [{}] {}\n",
        entry.timestamp,
        entry.level.to_uppercase(),
        message
    );

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("无法打开日志文件 {:?}: {e}", log_path))?;

    file.write_all(line.as_bytes())
        .map_err(|e| format!("写入日志失败: {e}"))?;

    if let Some(ref stack) = entry.stack {
        let stack_line = format!("  堆栈:\n{}\n", stack);
        file.write_all(stack_line.as_bytes())
            .map_err(|e| format!("写入堆栈失败: {e}"))?;
    }

    Ok(())
}

/// 获取应用日志目录路径。
///
/// 参数:
///     app: Tauri 应用句柄。
///
/// 返回:
///     平台规范下的应用日志目录路径。
///
/// 异常:
///     当 Tauri 无法解析日志目录时，返回错误字符串。
///
/// 副作用:
///     无。
pub(crate) fn get_log_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_log_dir()
        .map_err(|e| format!("获取应用日志目录失败: {e}"))
}
