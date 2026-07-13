//! IPC 命令定义。
//!
//! 包含：
//! - greet：示例命令（开发期验证 IPC 通道用）
//! - log_write：前端统一日志出口（经此写入应用日志目录下的 desktop.log）

use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use tauri::Manager;

/// 示例问候命令（开发期验证 IPC 通道用）。
#[tauri::command]
pub fn greet(name: &str) -> String {
    format!("你好, {}! 欢迎使用 Coding Agent。", name)
}

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

/**
 * 将前端日志条目追加写入 logs/desktop.log。
 *
 * 每次调用以 append 模式打开文件，写入格式化后的日志行。
 * 文件不存在时自动创建；目录不存在时创建目录树。
 */
#[tauri::command]
pub fn log_write(app: tauri::AppHandle, entry: LogEntry) -> Result<(), String> {
    let app_dir = get_log_dir(&app)?;
    let log_path = app_dir.join("desktop.log");

    // 确保父目录存在
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("无法创建日志目录: {e}"))?;
    }

    // 防止超大消息撑爆日志文件（截断到 8KB；调用方须确保不传入 secret）
    let message = if entry.message.len() > 8192 {
        format!("{}…(已截断)", &entry.message[..8192])
    } else {
        entry.message.clone()
    };

    // 格式化日志行：[timestamp] [LEVEL] message
    let line = format!(
        "[{}] [{}] {}\n",
        entry.timestamp,
        entry.level.to_uppercase(),
        message
    );

    // 追加写入
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("无法打开日志文件 {:?}: {e}", log_path))?;

    file.write_all(line.as_bytes())
        .map_err(|e| format!("写入日志失败: {e}"))?;

    // 如果有堆栈信息，额外缩进写入
    if let Some(ref stack) = entry.stack {
        let stack_line = format!("  堆栈:\n{}\n", stack);
        file.write_all(stack_line.as_bytes())
            .map_err(|e| format!("写入堆栈失败: {e}"))?;
    }

    Ok(())
}

/**
 * 获取应用日志目录路径。
 *
 * 使用 Tauri 提供的 `app.path().app_log_dir()` 解析系统级应用日志目录
 * （例如 macOS 的 `~/Library/Logs/<bundle_id>`）。路径由 Tauri 依据打包
 * 信息与平台规则确定，开发与生产环境一致、可预测，且不依赖当前工作目录，
 * 避免在 `src-tauri` 内写入触发 dev 重新编译。
 */
pub(crate) fn get_log_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_log_dir()
        .map_err(|e| format!("获取应用日志目录失败: {e}"))
}
