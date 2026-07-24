//! Tauri IPC 命令导出入口。
//!
//! 按职责拆分为：
//! - `logging`：前端统一日志落盘命令
//! - `backend`：本地后端托管命令
//! - `fs`：本地文件系统相关命令（如用系统应用打开文件）

pub mod backend;
pub mod fs;
pub mod logging;
