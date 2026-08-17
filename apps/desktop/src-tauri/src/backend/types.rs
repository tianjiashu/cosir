//! 本地后端托管共享类型。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// 后端生命周期状态枚举。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum BackendStatus {
    /// 后端未运行。
    Stopped,
    /// 后端正在启动。
    Starting,
    /// 后端健康可用。
    Running,
    /// 后端正在停止。
    Stopping,
    /// 后端正在重启。
    Restarting,
    /// 后端启动或运行失败。
    Failed,
}

/// 后端 `/health` 摘要。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct BackendHealthSnapshot {
    /// 健康端点返回的状态文本。
    pub status: String,
    /// 当前模型服务商。
    pub model_provider: String,
    /// 当前模型名称。
    pub model_name: String,
    /// 当前 thinking 模式。
    pub model_thinking_mode: String,
    /// 当前是否已拿到模型 API Key。
    pub has_model_api_key: bool,
}

/// 启动本地后端所需的运行时配置。
#[derive(Debug, Clone)]
pub struct BackendLaunchConfig {
    /// 仓库根目录。
    pub repo_root: PathBuf,
    /// Python 后端工作目录。
    pub backend_dir: PathBuf,
    /// Python 可执行文件路径。
    pub python_binary: PathBuf,
    /// 监听地址。
    pub host: String,
    /// 监听端口。
    pub port: u16,
    /// 后端应用日志路径。
    pub app_log_file: PathBuf,
    /// 后端标准输出日志路径。
    pub stdout_log_file: PathBuf,
    /// 后端标准错误日志路径。
    pub stderr_log_file: PathBuf,
    /// 后端启动状态文件路径（结构化启动就绪 / 失败契约）。
    pub boot_state_file: PathBuf,
}

/// 后端错误摘要。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct BackendErrorSummary {
    /// 错误发生阶段。
    pub stage: String,
    /// 面向用户的摘要消息。
    pub message: String,
    /// 更详细的错误描述。
    pub detail: String,
    /// 可选的原始错误堆栈（如后端启动崩溃的 traceback）。
    pub traceback: Option<String>,
    /// 错误发生时间。
    pub occurred_at: String,
}

/// 结构化后端状态响应。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatusResponse {
    /// 当前生命周期状态。
    pub status: BackendStatus,
    /// 当前后端是否由桌面端托管。
    pub managed: bool,
    /// 当前已知的进程 ID。
    pub pid: Option<u32>,
    /// 当前约定的监听端口。
    pub port: u16,
    /// 最近一次成功启动时间。
    pub started_at: Option<String>,
    /// 最近一次健康摘要。
    pub health: Option<BackendHealthSnapshot>,
    /// 最近一次结构化错误。
    pub last_error: Option<BackendErrorSummary>,
    /// 仓库根目录路径。
    pub repo_root: String,
    /// 后端工作目录路径。
    pub backend_dir: String,
    /// Python 可执行文件路径。
    pub python_binary: String,
}

/// 单个日志文件的尾部内容。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct BackendLogTailEntry {
    /// 日志文件绝对路径。
    pub path: String,
    /// 日志文件尾部文本。
    pub content: String,
}

/// 后端日志尾部响应。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct BackendLogsTailResponse {
    /// 返回的日志文件片段列表。
    pub entries: Vec<BackendLogTailEntry>,
}
