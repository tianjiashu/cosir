//! 本地后端托管状态机。

use crate::backend::health_checker::{fetch_backend_health, wait_for_backend_health};
use crate::backend::process_launcher::launch_backend_process;
use crate::backend::runtime_locator::resolve_backend_launch_config;
use crate::backend::types::{
    BackendErrorSummary, BackendHealthSnapshot, BackendLaunchConfig, BackendLogTailEntry,
    BackendLogsTailResponse, BackendStatus, BackendStatusResponse,
};
use chrono::Utc;
use std::fs;
use std::process::Child;
use std::sync::Mutex;
use std::time::Duration;

/// 全局后端 supervisor 状态容器。
pub struct BackendSupervisorState {
    inner: Mutex<BackendSupervisor>,
}

impl BackendSupervisorState {
    /// 创建新的全局后端 supervisor 状态。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     可被 Tauri `manage()` 持有的状态对象。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     无。
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(BackendSupervisor::new()),
        }
    }

    /// 启动本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     启动后的结构化后端状态。
    ///
    /// 异常:
    ///     当内部互斥锁中毒时返回错误字符串。
    ///
    /// 副作用:
    ///     可能创建 Python 子进程并写入日志文件。
    pub fn start(&self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let mut supervisor = self
            .inner
            .lock()
            .map_err(|_| "backend supervisor 锁已中毒".to_string())?;
        supervisor.start(app)
    }

    /// 停止本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     停止后的结构化后端状态。
    ///
    /// 异常:
    ///     当内部互斥锁中毒时返回错误字符串。
    ///
    /// 副作用:
    ///     可能终止 Python 子进程。
    pub fn stop(&self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let mut supervisor = self
            .inner
            .lock()
            .map_err(|_| "backend supervisor 锁已中毒".to_string())?;
        supervisor.stop(app)
    }

    /// 重启本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     重启后的结构化后端状态。
    ///
    /// 异常:
    ///     当内部互斥锁中毒时返回错误字符串。
    ///
    /// 副作用:
    ///     可能终止旧进程并创建新进程。
    pub fn restart(&self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let mut supervisor = self
            .inner
            .lock()
            .map_err(|_| "backend supervisor 锁已中毒".to_string())?;
        supervisor.restart(app)
    }

    /// 查询本地后端状态。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     当前结构化后端状态。
    ///
    /// 异常:
    ///     当内部互斥锁中毒时返回错误字符串。
    ///
    /// 副作用:
    ///     读取当前进程状态并访问 `/health`。
    pub fn status(&self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let mut supervisor = self
            .inner
            .lock()
            .map_err(|_| "backend supervisor 锁已中毒".to_string())?;
        supervisor.status(app)
    }

    /// 读取后端日志尾部。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///     max_lines: 每个日志文件最多返回的行数。
    ///
    /// 返回:
    ///     日志尾部片段。
    ///
    /// 异常:
    ///     当内部互斥锁中毒时返回错误字符串。
    ///
    /// 副作用:
    ///     读取本地日志文件。
    pub fn logs_tail(
        &self,
        app: &tauri::AppHandle,
        max_lines: usize,
    ) -> Result<BackendLogsTailResponse, String> {
        let mut supervisor = self
            .inner
            .lock()
            .map_err(|_| "backend supervisor 锁已中毒".to_string())?;
        supervisor.logs_tail(app, max_lines)
    }
}

/// 已托管的后端子进程信息。
#[derive(Debug)]
struct ManagedBackendProcess {
    /// Python 子进程句柄。
    child: Child,
    /// 启动时间。
    started_at: String,
}

/// 本地后端托管器。
#[derive(Debug)]
struct BackendSupervisor {
    /// 当前生命周期状态。
    status: BackendStatus,
    /// 当前被桌面端托管的子进程。
    managed_process: Option<ManagedBackendProcess>,
    /// 最近一次健康快照。
    last_health: Option<BackendHealthSnapshot>,
    /// 最近一次结构化错误。
    last_error: Option<BackendErrorSummary>,
}

impl BackendSupervisor {
    /// 创建一个新的后端托管器。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     初始状态为 `Stopped` 的托管器。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     无。
    fn new() -> Self {
        Self {
            status: BackendStatus::Stopped,
            managed_process: None,
            last_health: None,
            last_error: None,
        }
    }

    /// 启动本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     启动后的结构化后端状态。
    ///
    /// 异常:
    ///     当运行时路径无法解析时返回错误字符串。
    ///
    /// 副作用:
    ///     可能创建子进程并轮询健康检查。
    fn start(&mut self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let config = resolve_backend_launch_config(app)?;
        self.refresh_with_config(&config);

        if self.status == BackendStatus::Running {
            return Ok(self.build_response(&config));
        }

        self.status = BackendStatus::Starting;
        self.last_error = None;

        let launched = match launch_backend_process(&config) {
            Ok(launched) => launched,
            Err(detail) => {
                self.record_error("launch", "启动本地后端失败", detail);
                self.status = BackendStatus::Failed;
                return Ok(self.build_response(&config));
            }
        };

        let started_at = launched.started_at.clone();
        self.managed_process = Some(ManagedBackendProcess {
            child: launched.child,
            started_at,
        });

        match wait_for_backend_health(&config, Duration::from_secs(15)) {
            Ok(snapshot) => {
                self.last_health = Some(snapshot);
                self.last_error = None;
                self.status = BackendStatus::Running;
            }
            Err(detail) => {
                self.record_error("health_check", "后端启动后未通过健康检查", detail);
                self.status = BackendStatus::Failed;
                self.terminate_managed_process();
            }
        }

        Ok(self.build_response(&config))
    }

    /// 停止当前托管的本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     停止后的结构化后端状态。
    ///
    /// 异常:
    ///     当运行时路径无法解析时返回错误字符串。
    ///
    /// 副作用:
    ///     可能终止一个已托管子进程。
    fn stop(&mut self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let config = resolve_backend_launch_config(app)?;
        self.refresh_with_config(&config);

        if self.managed_process.is_none() {
            if self.status == BackendStatus::Running {
                self.record_error(
                    "stop",
                    "当前后端未由桌面端托管，无法远程停止",
                    "检测到 /health 可用，但没有可终止的受管进程".to_string(),
                );
            } else {
                self.status = BackendStatus::Stopped;
            }
            return Ok(self.build_response(&config));
        }

        self.status = BackendStatus::Stopping;
        self.terminate_managed_process();
        self.last_health = fetch_backend_health(&config)?;
        self.status = if self.last_health.is_some() {
            BackendStatus::Running
        } else {
            BackendStatus::Stopped
        };
        Ok(self.build_response(&config))
    }

    /// 重启当前托管的本地后端。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     重启后的结构化后端状态。
    ///
    /// 异常:
    ///     当运行时路径无法解析时返回错误字符串。
    ///
    /// 副作用:
    ///     可能终止旧进程并创建新进程。
    fn restart(&mut self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        self.status = BackendStatus::Restarting;
        self.terminate_managed_process();
        self.last_health = None;
        self.start(app)
    }

    /// 查询当前后端状态。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///
    /// 返回:
    ///     当前结构化后端状态。
    ///
    /// 异常:
    ///     当运行时路径无法解析时返回错误字符串。
    ///
    /// 副作用:
    ///     读取当前受管进程状态并访问 `/health`。
    fn status(&mut self, app: &tauri::AppHandle) -> Result<BackendStatusResponse, String> {
        let config = resolve_backend_launch_config(app)?;
        self.refresh_with_config(&config);
        Ok(self.build_response(&config))
    }

    /// 读取后端日志尾部。
    ///
    /// 参数:
    ///     app: Tauri 应用句柄。
    ///     max_lines: 每个日志文件最多返回的行数。
    ///
    /// 返回:
    ///     多个日志文件的尾部内容。
    ///
    /// 异常:
    ///     当运行时路径无法解析时返回错误字符串。
    ///
    /// 副作用:
    ///     读取本地日志文件。
    fn logs_tail(
        &mut self,
        app: &tauri::AppHandle,
        max_lines: usize,
    ) -> Result<BackendLogsTailResponse, String> {
        let config = resolve_backend_launch_config(app)?;
        let mut entries = Vec::new();
        for path in [
            config.app_log_file.clone(),
            config.stdout_log_file.clone(),
            config.stderr_log_file.clone(),
        ] {
            if path.exists() {
                entries.push(BackendLogTailEntry {
                    path: path.display().to_string(),
                    content: read_tail_lines(&path, max_lines)?,
                });
            }
        }

        Ok(BackendLogsTailResponse { entries })
    }

    /// 同步受管进程与健康状态。
    ///
    /// 参数:
    ///     config: 当前运行时配置。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无；健康检查失败会被吞掉并反映到状态字段。
    ///
    /// 副作用:
    ///     可能清理已退出的子进程，并刷新最近健康快照。
    fn refresh_with_config(&mut self, config: &BackendLaunchConfig) {
        if self.managed_process_exited() {
            self.managed_process = None;
        }

        match fetch_backend_health(config) {
            Ok(Some(snapshot)) => {
                self.last_health = Some(snapshot);
                self.status = BackendStatus::Running;
            }
            Ok(None) => {
                self.last_health = None;
                if self.managed_process.is_none() && self.status != BackendStatus::Failed {
                    self.status = BackendStatus::Stopped;
                }
            }
            Err(detail) => {
                self.record_error("health_check", "读取后端健康状态失败", detail);
                self.status = BackendStatus::Failed;
            }
        }
    }

    /// 判断受管进程是否已经退出。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     受管进程存在且已经退出时返回 `true`。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     会调用子进程 `try_wait()`。
    fn managed_process_exited(&mut self) -> bool {
        let Some(process) = self.managed_process.as_mut() else {
            return false;
        };

        match process.child.try_wait() {
            Ok(Some(_)) => true,
            Ok(None) => false,
            Err(detail) => {
                self.record_error(
                    "process_state",
                    "读取本地后端进程状态失败",
                    detail.to_string(),
                );
                true
            }
        }
    }

    /// 终止当前托管子进程。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无；终止失败会写入结构化错误。
    ///
    /// 副作用:
    ///     可能向子进程发送 kill 并等待回收。
    fn terminate_managed_process(&mut self) {
        let Some(mut process) = self.managed_process.take() else {
            return;
        };

        if let Err(detail) = process.child.kill() {
            self.record_error(
                "stop",
                "终止本地后端进程失败",
                detail.to_string(),
            );
            return;
        }

        if let Err(detail) = process.child.wait() {
            self.record_error(
                "stop",
                "等待本地后端进程退出失败",
                detail.to_string(),
            );
        }
    }

    /// 构建对前端可消费的状态响应。
    ///
    /// 参数:
    ///     config: 当前运行时配置。
    ///
    /// 返回:
    ///     结构化后端状态响应。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     无。
    fn build_response(&self, config: &BackendLaunchConfig) -> BackendStatusResponse {
        BackendStatusResponse {
            status: self.status.clone(),
            managed: self.managed_process.is_some(),
            pid: self.managed_process.as_ref().map(|process| process.child.id()),
            port: config.port,
            started_at: self
                .managed_process
                .as_ref()
                .map(|process| process.started_at.clone()),
            health: self.last_health.clone(),
            last_error: self.last_error.clone(),
            repo_root: config.repo_root.display().to_string(),
            backend_dir: config.backend_dir.display().to_string(),
            python_binary: config.python_binary.display().to_string(),
        }
    }

    /// 记录结构化错误摘要。
    ///
    /// 参数:
    ///     stage: 错误发生阶段。
    ///     message: 面向用户的摘要消息。
    ///     detail: 详细错误文本。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     更新最近一次错误状态。
    fn record_error(&mut self, stage: &str, message: &str, detail: String) {
        self.last_error = Some(BackendErrorSummary {
            stage: stage.to_string(),
            message: message.to_string(),
            detail,
            occurred_at: Utc::now().to_rfc3339(),
        });
    }
}

/// 读取文件尾部若干行。
///
/// 参数:
///     path: 日志文件路径。
///     max_lines: 最多返回的尾部行数。
///
/// 返回:
///     按原始顺序拼接的尾部文本。
///
/// 异常:
///     当文件无法读取时返回错误字符串。
///
/// 副作用:
///     读取本地文件内容。
fn read_tail_lines(path: &std::path::Path, max_lines: usize) -> Result<String, String> {
    let content = fs::read_to_string(path)
        .map_err(|e| format!("无法读取日志文件 {}: {e}", path.display()))?;
    let lines: Vec<&str> = content.lines().collect();
    let start = lines.len().saturating_sub(max_lines);
    Ok(lines[start..].join("\n"))
}

impl Drop for BackendSupervisor {
    /// 在应用释放 supervisor 时清理子进程。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     尝试终止当前受管 Python 子进程。
    fn drop(&mut self) {
        self.terminate_managed_process();
    }
}
