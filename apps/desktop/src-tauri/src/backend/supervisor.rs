//! 本地后端托管状态机。

use crate::backend::boot_state::{read_boot_state, BackendBootState};
use crate::backend::health_checker::fetch_backend_health;
use crate::backend::process_launcher::launch_backend_process;
use crate::backend::runtime_locator::resolve_backend_launch_config;
use crate::backend::types::{
    BackendErrorSummary, BackendHealthSnapshot, BackendLaunchConfig, BackendLogTailEntry,
    BackendLogsTailResponse, BackendStatus, BackendStatusResponse,
};
use chrono::Utc;
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::Child;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// 解析后端启动配置；解析失败时尽力将错误写入 supervisor 失败路径日志。
///
/// 参数:
///     app: Tauri 应用句柄。
///
/// 返回:
///     成功时返回 `BackendLaunchConfig`；失败时返回原始错误字符串（已落盘）。
///
/// 异常:
///     无。
///
/// 副作用:
///     配置解析失败时向 `logs/backend-supervisor.log` 追加一条错误记录。
fn resolve_config_logged(app: &tauri::AppHandle) -> Result<BackendLaunchConfig, String> {
    match resolve_backend_launch_config(app) {
        Ok(config) => Ok(config),
        Err(err) => {
            log_config_resolution_failure("resolve_config", &err);
            Err(err)
        }
    }
}

/// 在配置解析失败（尚无法确定日志目录）时，尽力将错误写入仓库 logs 目录。
///
/// 参数:
///     stage: 错误发生阶段。
///     message: 错误描述。
///
/// 返回:
///     无。
///
/// 异常:
///     无；写入失败被静默忽略。
///
/// 副作用:
///     向 `<repo_root>/logs/backend-supervisor.log` 追加一条错误记录。
fn log_config_resolution_failure(stage: &str, message: &str) {
    let repo_root = crate::backend::runtime_locator::resolve_repo_root()
        .unwrap_or_else(|_| PathBuf::from("."));
    let log_path = repo_root.join("logs").join("backend-supervisor.log");
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let ts = chrono::Utc::now().to_rfc3339();
    let line = format!("[{ts}] [ERROR] [backend:{stage}] {message}\n");
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .and_then(|mut file| file.write_all(line.as_bytes()));
}

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
    /// 后端失败路径落盘日志路径（如 `logs/backend-supervisor.log`）；未解析配置时为 `None`。
    error_log_path: Option<PathBuf>,
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
            error_log_path: None,
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
        let config = resolve_config_logged(app)?;
        self.refresh_with_config(&config);

        if self.status == BackendStatus::Running {
            return Ok(self.build_response(&config));
        }

        self.status = BackendStatus::Starting;
        self.last_error = None;

        let launched = match launch_backend_process(&config) {
            Ok(launched) => launched,
            Err(detail) => {
                self.record_error("launch", "启动本地后端失败", detail, None);
                self.status = BackendStatus::Failed;
                return Ok(self.build_response(&config));
            }
        };

        let started_at = launched.started_at.clone();
        self.managed_process = Some(ManagedBackendProcess {
            child: launched.child,
            started_at,
        });

        match self.wait_for_backend_ready(&config, Duration::from_secs(15)) {
            Ok(snapshot) => {
                self.last_health = Some(snapshot);
                self.last_error = None;
                self.status = BackendStatus::Running;
            }
            Err(detail) => {
                // 优先使用启动状态文件中的结构化失败原因（若后端确实启动失败）。
                if let Some(boot) = read_boot_state(&config) {
                    if boot.phase_kind() == crate::backend::boot_state::BootPhase::Failed {
                        // 把失败发生的子阶段（如 start / app_ready）附到 detail，便于定位。
                        let mut detail = format_boot_detail(&boot);
                        if let Some(step) = &boot.step {
                            detail = format!("{detail}（阶段：{step}）");
                        }
                        self.record_error(
                            "bootstrap",
                            "后端启动失败",
                            detail,
                            boot.traceback.clone(),
                        );
                    } else {
                        self.record_error(
                            "health_check",
                            "后端启动后未通过健康检查",
                            detail,
                            None,
                        );
                    }
                } else {
                    self.record_error(
                        "health_check",
                        "后端启动后未通过健康检查",
                        detail,
                        None,
                    );
                }
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
        let config = resolve_config_logged(app)?;
        self.refresh_with_config(&config);

        if self.managed_process.is_none() {
            if self.status == BackendStatus::Running {
                self.record_error(
                    "stop",
                    "当前后端未由桌面端托管，无法远程停止",
                    "检测到 /health 可用，但没有可终止的受管进程".to_string(),
                    None,
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
        let config = resolve_config_logged(app)?;
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
        let config = resolve_config_logged(app)?;
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

    /// 等待后端就绪，优先依据启动状态文件做快速失败与就绪判断。
    ///
    /// 与单纯轮询 `/health` 不同，本方法在每轮轮询中：
    /// 1. 若受管进程已退出，立即失败（不再傻等固定超时）；
    /// 2. 若启动状态文件标记为 `failed`，立即失败；
    /// 3. 若启动状态文件标记为 `ready`，再回退到 `/health` 确认端口可用。
    ///
    /// 参数:
    ///     config: 后端启动配置。
    ///     timeout: 最大等待时长。
    ///
    /// 返回:
    ///     超时前探测到健康响应时返回摘要。
    ///
    /// 异常:
    ///     当超时、进程提前退出或启动状态文件标记为失败时返回错误字符串。
    ///
    /// 副作用:
    ///     在等待窗口内重复读取启动状态文件并访问 `/health` 端点。
    fn wait_for_backend_ready(
        &mut self,
        config: &BackendLaunchConfig,
        timeout: Duration,
    ) -> Result<BackendHealthSnapshot, String> {
        let started_at = Instant::now();

        while started_at.elapsed() < timeout {
            // 进程已退出：立即失败，避免傻等满超时。
            if self.managed_process_exited() {
                self.managed_process = None;
                return Err("本地后端进程在启动期间退出".to_string());
            }

            // 启动状态文件给出明确信号时优先处理。
            if let Some(boot) = read_boot_state(config) {
                match boot.phase_kind() {
                    crate::backend::boot_state::BootPhase::Failed => {
                        return Err("本地后端启动失败（详见启动状态）".to_string());
                    }
                    crate::backend::boot_state::BootPhase::Ready => {
                        if let Some(snapshot) = fetch_backend_health(config)? {
                            return Ok(snapshot);
                        }
                    }
                    _ => {}
                }
            }

            std::thread::sleep(Duration::from_millis(250));
        }

        Err(format!(
            "等待本地后端健康检查超时（{} 秒）",
            timeout.as_secs()
        ))
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
        // 解析失败路径日志落盘位置（与后端应用日志同目录）。
        self.error_log_path = Some(
            config
                .app_log_file
                .parent()
                .map(|parent| parent.join("backend-supervisor.log"))
                .unwrap_or_else(|| PathBuf::from("backend-supervisor.log")),
        );

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
                self.record_error("health_check", "读取后端健康状态失败", detail, None);
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
                    None,
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
                None,
            );
            return;
        }

        if let Err(detail) = process.child.wait() {
            self.record_error(
                "stop",
                "等待本地后端进程退出失败",
                detail.to_string(),
                None,
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
    ///     traceback: 可选的原始错误堆栈（如后端启动崩溃的 traceback）。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无。
    ///
    /// 副作用:
    ///     更新最近一次错误状态。
    fn record_error(
        &mut self,
        stage: &str,
        message: &str,
        detail: String,
        traceback: Option<String>,
    ) {
        // 跨进程传递的 traceback 可能含敏感信息（如 API Key 片段），落盘与展示前脱敏。
        let safe_traceback = traceback.map(|raw| redact_sensitive_text(&raw));
        let summary = BackendErrorSummary {
            stage: stage.to_string(),
            message: message.to_string(),
            detail,
            traceback: safe_traceback.clone(),
            occurred_at: Utc::now().to_rfc3339(),
        };
        self.last_error = Some(summary.clone());
        // 失败路径必须同步落盘，保证本地可排查（规范第六章）。
        self.append_error_log(&summary);
    }

    /// 将结构化错误追加写入后端 supervisor 失败路径日志。
    ///
    /// 参数:
    ///     summary: 已脱敏的结构化错误摘要。
    ///
    /// 返回:
    ///     无。
    ///
    /// 异常:
    ///     无；写入失败被静默忽略，不影响主流程。
    ///
    /// 副作用:
    ///     创建日志目录并向 `backend-supervisor.log` 追加一行错误记录。
    fn append_error_log(&self, summary: &BackendErrorSummary) {
        let Some(ref path) = self.error_log_path else {
            return;
        };
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let traceback_tail = summary
            .traceback
            .as_ref()
            .map(|tb| {
                let head: String = tb.lines().take(3).collect::<Vec<_>>().join(" | ");
                format!(" | traceback={head}")
            })
            .unwrap_or_default();
        let line = format!(
            "[{}] [ERROR] [backend:{}] {} | detail={}{}\n",
            summary.occurred_at, summary.stage, summary.message, summary.detail, traceback_tail
        );
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .and_then(|mut file| file.write_all(line.as_bytes()));
    }
}

/// 将启动状态中的失败信息格式化为面向用户的 detail 文本。
///
/// 参数:
///     boot: 后端启动状态（应为 `failed` 阶段）。
///
/// 返回:
///     合并异常类型与消息的简短描述；两者皆缺时返回兜底文本。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn format_boot_detail(boot: &BackendBootState) -> String {
    match (&boot.error_type, &boot.error_message) {
        (Some(error_type), Some(error_message)) => format!("{error_type}: {error_message}"),
        (Some(error_type), None) => error_type.clone(),
        (None, Some(error_message)) => error_message.clone(),
        (None, None) => "未知启动错误".to_string(),
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

/// 需要脱敏的敏感键名（大小写不敏感）。
const SENSITIVE_KEYS: &[&str] = &[
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "access_key",
    "accesskey",
    "private_key",
    "privatekey",
    "credential",
    "authorization",
];

/// 对可能含敏感信息的自由文本（如 Python traceback）做脱敏。
///
/// 屏蔽两类高风险内容：
/// 1. `sk-` 前缀的 API Key（DeepSeek / OpenAI 等大模型密钥）；
/// 2. 敏感键（api_key / token / secret / password 等）的赋值片段。
///
/// 参数:
///     text: 原始文本。
///
/// 返回:
///     脱敏后的文本；非敏感内容原样保留。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn redact_sensitive_text(text: &str) -> String {
    // 先处理敏感键赋值，再处理裸 `sk-` 前缀密钥。
    // 顺序很关键：若先注入 `sk-[REDACTED]` 占位符，再跑赋值脱敏时占位符中的
    // `]` 会被当作值边界，产生 `[REDACTED]]` 畸形输出。
    let mut out = text.to_string();
    for key in SENSITIVE_KEYS {
        out = mask_sensitive_assignment(&out, key);
    }
    mask_api_key_literals(&out)
}

/// 屏蔽 `sk-` 前缀的 API Key 字面量。
///
/// 参数:
///     text: 原始文本。
///
/// 返回:
///     `sk-` 后连续密钥片段被替换为 `[REDACTED]` 的文本。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn mask_api_key_literals(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        // 大小写不敏感匹配 `sk-` 前缀（覆盖 SK-/Sk-/sK-），末尾恰为 `sk-` 也屏蔽。
        if i + 2 < chars.len()
            && (chars[i] == 's' || chars[i] == 'S')
            && (chars[i + 1] == 'k' || chars[i + 1] == 'K')
            && chars[i + 2] == '-'
        {
            out.push_str("sk-[REDACTED]");
            i += 3;
            while i < chars.len() && !is_token_boundary(chars[i]) {
                i += 1;
            }
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// 屏蔽形如 `key="value"` / `key: 'value'` / `key=value` 中的敏感值。
///
/// 参数:
///     text: 原始文本。
///     key: 敏感键名（小写比较）。
///
/// 返回:
///     敏感值被替换为 `[REDACTED]` 的文本；未匹配赋值模式时原样保留。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn mask_sensitive_assignment(text: &str, key: &str) -> String {
    // 使用 ASCII 小写，保证 `lowered` 与 `text` 字节长度一致，避免索引错位。
    let key_lower = key.to_ascii_lowercase();
    let lowered = text.to_ascii_lowercase();
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut search_start = 0;
    while let Some(pos) = lowered[search_start..].find(&key_lower) {
        let at = search_start + pos;
        let after = at + key.len();
        // 跳过键名与赋值符之间的空白，定位赋值符。
        let mut j = after;
        while j < text.len() && (bytes[j] == b' ' || bytes[j] == b'\t') {
            j += 1;
        }
        // 非赋值场景（如键名作为子串出现），原样输出键名并继续搜索。
        if j >= text.len() || (bytes[j] != b'=' && bytes[j] != b':') {
            out.push_str(&text[search_start..after]);
            search_start = after;
            continue;
        }
        // 输出键名前普通文本 + 键名 + 赋值符（含之间空白）。
        out.push_str(&text[search_start..=j]);
        let mut k = j + 1;
        // 跳过赋值符后的空白（保留到输出，维持可读性）。
        while k < text.len() && (bytes[k] == b' ' || bytes[k] == b'\t') {
            out.push(bytes[k] as char);
            k += 1;
        }
        if k < text.len() && (bytes[k] == b'"' || bytes[k] == b'\'') {
            // 引号感知：引号内的值整体脱敏（允许含空格）。
            let quote = bytes[k];
            out.push(quote as char);
            out.push_str("[REDACTED]");
            k += 1;
            // 消费到匹配的闭合引号；未闭合时消费到文本末尾，杜绝残留明文。
            while k < text.len() && bytes[k] != quote {
                k += 1;
            }
            if k < text.len() {
                // 源文本存在闭合引号，跳过它（已由输出侧统一补回）。
                k += 1;
            }
            // 无论源文本是否闭合，输出侧都补上闭合引号，与 Python 端形态保持一致，
            // 便于下游按引号配对解析。
            out.push(quote as char);
            search_start = k;
        } else {
            // 无引号值：按单 token 截断到边界。
            out.push_str("[REDACTED]");
            while k < text.len() {
                let ch = text[k..].chars().next().unwrap();
                if is_token_boundary(ch) {
                    break;
                }
                k += ch.len_utf8();
            }
            search_start = k;
        }
    }
    out.push_str(&text[search_start..]);
    out
}

/// 判断字符是否为令牌边界（用于截断脱敏值）。
///
/// 参数:
///     c: 待判断字符。
///
/// 返回:
///     是边界返回 `true`。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn is_token_boundary(c: char) -> bool {
    c.is_whitespace() || c == '"' || c == '\'' || c == ')' || c == ',' || c == ';' || c == '}'
        || c == ']' || c == '\n' || c == '\r'
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造一个用于测试的最小 `BackendBootState`。
    fn make_boot(
        phase: &str,
        step: Option<&str>,
        error_type: Option<&str>,
        error_message: Option<&str>,
        traceback: Option<&str>,
    ) -> BackendBootState {
        BackendBootState {
            phase: phase.to_string(),
            step: step.map(|s| s.to_string()),
            error_type: error_type.map(|s| s.to_string()),
            error_message: error_message.map(|s| s.to_string()),
            traceback: traceback.map(|s| s.to_string()),
        }
    }

    #[test]
    fn test_format_boot_detail_both_present() {
        let boot = make_boot("failed", Some("start"), Some("RuntimeError"), Some("boom"), None);
        assert_eq!(format_boot_detail(&boot), "RuntimeError: boom");
    }

    #[test]
    fn test_format_boot_detail_only_type() {
        let boot = make_boot("failed", None, Some("ValueError"), None, None);
        assert_eq!(format_boot_detail(&boot), "ValueError");
    }

    #[test]
    fn test_format_boot_detail_only_message() {
        let boot = make_boot("failed", None, None, Some("something broke"), None);
        assert_eq!(format_boot_detail(&boot), "something broke");
    }

    #[test]
    fn test_format_boot_detail_neither() {
        let boot = make_boot("failed", None, None, None, None);
        assert_eq!(format_boot_detail(&boot), "未知启动错误");
    }

    #[test]
    fn test_read_tail_lines_normal() {
        let dir = std::env::temp_dir().join("coding_agent_test_logs");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("tail_normal.log");
        std::fs::write(&path, "line1\nline2\nline3\nline4\n").unwrap();
        let out = read_tail_lines(&path, 2).unwrap();
        assert_eq!(out, "line3\nline4");
    }

    #[test]
    fn test_read_tail_lines_more_than_file() {
        let dir = std::env::temp_dir().join("coding_agent_test_logs");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("tail_small.log");
        std::fs::write(&path, "only\n").unwrap();
        // max_lines 超过文件行数时不应 panic（saturating_sub）。
        let out = read_tail_lines(&path, 100).unwrap();
        assert_eq!(out, "only");
    }

    #[test]
    fn test_read_tail_lines_zero_lines() {
        let dir = std::env::temp_dir().join("coding_agent_test_logs");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("tail_zero.log");
        std::fs::write(&path, "a\nb\nc\n").unwrap();
        let out = read_tail_lines(&path, 0).unwrap();
        assert_eq!(out, "");
    }

    #[test]
    fn test_read_tail_lines_missing_file() {
        let path = std::env::temp_dir().join("coding_agent_test_logs/does_not_exist.log");
        let result = read_tail_lines(&path, 10);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("无法读取日志文件"));
    }

    #[test]
    fn test_record_error_sets_last_error() {
        let mut supervisor = BackendSupervisor::new();
        assert!(supervisor.last_error.is_none());
        supervisor.record_error("launch", "启动失败", "detail text".to_string(), Some("tb".to_string()));
        let err = supervisor.last_error.as_ref().unwrap();
        assert_eq!(err.stage, "launch");
        assert_eq!(err.message, "启动失败");
        assert_eq!(err.detail, "detail text");
        assert_eq!(err.traceback.as_deref(), Some("tb"));
        assert!(!err.occurred_at.is_empty());
    }

    #[test]
    fn test_record_error_without_traceback() {
        let mut supervisor = BackendSupervisor::new();
        supervisor.record_error("health_check", "健康检查失败", "no detail".to_string(), None);
        let err = supervisor.last_error.as_ref().unwrap();
        assert_eq!(err.traceback, None);
    }

    #[test]
    fn test_redact_sensitive_text_masks_api_key() {
        let input = "Connection failed with REDACTED_DEEPSEEK_KEY in trace";
        let out = redact_sensitive_text(input);
        assert!(!out.contains("REDACTED_DEEPSEEK_KEY"));
        assert!(out.contains("sk-[REDACTED]"));
    }

    #[test]
    fn test_redact_sensitive_text_masks_assignment() {
        let input = r#"config loaded api_key="secret-value-123" token='abc' normal ok"#;
        let out = redact_sensitive_text(input);
        assert!(out.contains(r#"api_key="[REDACTED]""#));
        assert!(out.contains("token='[REDACTED]'"));
        assert!(out.contains("normal ok"));
    }

    #[test]
    fn test_redact_sensitive_text_preserves_normal_text() {
        let input = "Traceback (most recent call last): File '/path/main.py', line 10";
        let out = redact_sensitive_text(input);
        assert_eq!(out, input);
    }

    #[test]
    fn test_redact_sensitive_text_masks_quoted_value_with_spaces() {
        // 引号内含空格的敏感值必须整体脱敏，不得残留任何片段。
        let input = r#"config password="my secret pass phrase" then normal ok"#;
        let out = redact_sensitive_text(input);
        assert!(!out.contains("my secret pass phrase"));
        assert!(!out.contains("secret pass"));
        assert!(!out.contains("phrase"));
        assert!(out.contains(r#"password="[REDACTED]""#));
        assert!(out.contains("normal ok"));
    }

    #[test]
    fn test_redact_sensitive_text_does_not_over_match_substring() {
        // `token` 作为 `tokenizer` 子串且非赋值时不应被脱敏。
        let input = "loading tokenizer=bert done";
        let out = redact_sensitive_text(input);
        assert!(out.contains("tokenizer="));
    }

    #[test]
    fn test_redact_sensitive_text_unclosed_quote_not_leaked() {
        // 未闭合引号的敏感值不得残留明文，且输出补回闭合引号，
        // 与 Python 端 `_redact_assignment` 形态一致（`key="[REDACTED]"`）。
        let input = "fatal password=\"oops then rest of line";
        let out = redact_sensitive_text(input);
        assert!(!out.contains("oops then rest"));
        assert!(!out.contains("oops"));
        assert!(out.contains(r#"password="[REDACTED]""#));
    }

    #[test]
    fn test_redact_sensitive_text_sk_in_assignment_no_malformed() {
        // `api_key=sk-...` 复合场景应完整脱敏且不产生 `]]` 畸形输出。
        let input = "auth api_key=REDACTED_DEEPSEEK_KEY extra";
        let out = redact_sensitive_text(input);
        assert!(!out.contains("REDACTED_DEEPSEEK_KEY"));
        assert!(!out.contains("]]"));
        assert!(out.contains("api_key=[REDACTED]"));
        assert!(out.contains("extra"));
    }

    #[test]
    fn test_redact_sensitive_text_masks_uppercase_api_key() {
        // 大写 SK- 前缀密钥同样需要脱敏。
        let input = "connect with SK-e920522a28a844c2be0d4581f4d9c650 done";
        let out = redact_sensitive_text(input);
        assert!(!out.contains("SK-e920522a28a844c2be0d4581f4d9c650"));
        assert!(!out.contains("e920522a28a844c2be0d4581f4d9c650"));
        assert!(out.contains("sk-[REDACTED]"));
    }

    #[test]
    fn test_redact_sensitive_text_masks_trailing_api_key() {
        // 末尾恰为完整 sk- 密钥（无尾随字符）也需脱敏。
        let input = "leaked key REDACTED_DEEPSEEK_KEY";
        let out = redact_sensitive_text(input);
        assert!(!out.contains("REDACTED_DEEPSEEK_KEY"));
        assert!(out.contains("sk-[REDACTED]"));
    }

    #[test]
    fn test_record_error_redacts_traceback_before_store() {
        let mut supervisor = BackendSupervisor::new();
        supervisor.record_error(
            "bootstrap",
            "后端启动失败",
            "detail".to_string(),
            Some("auth api_key=REDACTED_DEEPSEEK_KEY leaked".to_string()),
        );
        let err = supervisor.last_error.as_ref().unwrap();
        let tb = err.traceback.as_deref().unwrap();
        // 原始密钥不得残留；敏感值必须被脱敏。
        assert!(!tb.contains("REDACTED_DEEPSEEK_KEY"));
        assert!(tb.contains("[REDACTED]"));
    }
}
