use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::backend_process::{
    spawn_backend, terminate_process_tree, BackendLaunchConfig, BackendProcess,
};
use crate::backend_readiness::wait_for_backend;
use crate::backend_runtime::resolve_backend_runtime;
use crate::desktop_log::append_json_line;

const HOST: &str = "127.0.0.1";
const READY_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_CRASH_RECOVERY_ATTEMPTS: usize = 1;

fn should_attempt_crash_recovery(attempt: usize) -> bool {
    attempt < MAX_CRASH_RECOVERY_ATTEMPTS
}

fn is_current_generation(current: usize, expected: usize) -> bool {
    current == expected
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "snake_case", tag = "state")]
pub enum BackendStatus {
    Starting,
    Ready,
    Failed { message: String },
    Stopped,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendRuntimeConfig {
    pub backend_base_url: String,
    pub status: BackendStatus,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FrontendLogEntry {
    ts: String,
    level: String,
    logger: String,
    trace_id: String,
    caller: String,
    event: String,
    msg: String,
    data: serde_json::Value,
    error: Option<serde_json::Value>,
    truncated: bool,
}

struct BackendInner {
    child: Mutex<Option<ManagedBackendProcess>>,
    lifecycle_gate: Mutex<()>,
    shutting_down: AtomicBool,
    crash_recovery_attempts: AtomicUsize,
    status: Mutex<BackendStatus>,
    log_file: Mutex<Option<PathBuf>>,
    backend_base_url: Mutex<Option<String>>,
    start_in_progress: AtomicBool,
    lifecycle_generation: AtomicUsize,
}

#[derive(Clone)]
pub struct BackendSupervisor {
    inner: Arc<BackendInner>,
}

impl Default for BackendSupervisor {
    fn default() -> Self {
        Self {
            inner: Arc::new(BackendInner {
                child: Mutex::new(None),
                lifecycle_gate: Mutex::new(()),
                shutting_down: AtomicBool::new(false),
                crash_recovery_attempts: AtomicUsize::new(0),
                status: Mutex::new(BackendStatus::Stopped),
                log_file: Mutex::new(None),
                backend_base_url: Mutex::new(None),
                start_in_progress: AtomicBool::new(false),
                lifecycle_generation: AtomicUsize::new(0),
            }),
        }
    }
}

impl BackendSupervisor {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        match self.start_inner(app) {
            Ok(()) => Ok(()),
            Err(error)
                if error == "本地 Agent 后端正在启动"
                    || error == "后端启动已被新的生命周期操作取消" =>
            {
                Err(error)
            }
            Err(error) => self.fail(error),
        }
    }

    fn start_inner(&self, app: &AppHandle) -> Result<(), String> {
        let lifecycle_guard = self
            .inner
            .lifecycle_gate
            .lock()
            .map_err(|_| "后端生命周期锁已损坏".to_string())?;
        let start_reserved = self
            .inner
            .start_in_progress
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok();
        drop(lifecycle_guard);
        if !start_reserved {
            return Err("本地 Agent 后端正在启动".to_string());
        }
        let _start_guard = StartGuard(&self.inner.start_in_progress);
        let generation = self.inner.lifecycle_generation.load(Ordering::Acquire);
        if self
            .inner
            .child
            .lock()
            .map_err(|_| "后端进程锁已损坏".to_string())?
            .is_some()
        {
            return Ok(());
        }
        let runtime_dir = app
            .path()
            .app_data_dir()
            .map_err(|error| format!("无法解析 Cosir 数据目录：{error}"))?
            .join("runtime");
        std::fs::create_dir_all(&runtime_dir)
            .map_err(|error| format!("无法创建运行时目录：{error}"))?;
        let bootstate = runtime_dir.join("backend.bootstate.json");
        *self
            .inner
            .log_file
            .lock()
            .map_err(|_| "日志锁已损坏".to_string())? = Some(runtime_dir.join("desktop.log"));
        self.set_status(BackendStatus::Starting);
        let backend_runtime = match resolve_backend_runtime(app, &runtime_dir) {
            Ok(runtime) => runtime,
            Err(error) => {
                if self.inner.lifecycle_generation.load(Ordering::Acquire) != generation {
                    return Err("后端启动已被新的生命周期操作取消".to_string());
                }
                return Err(error);
            }
        };
        let log_file = runtime_dir.join("backend-console.log");
        let mut last_error = "本地 Agent 后端启动失败".to_string();
        for attempt in 0..3 {
            if attempt > 0 {
                // 重试属于当前生命周期；不要调用 prepare_start，因为它会
                // 复位 shutting_down，可能把并发 stop 误打开。generation 只
                // 在真正的 start/restart 操作开始时推进。
                let _ = std::fs::remove_file(&bootstate);
            }
            if self.inner.shutting_down.load(Ordering::Acquire) {
                return Ok(());
            }
            if self.inner.lifecycle_generation.load(Ordering::Acquire) != generation {
                return Err("后端启动已被新的生命周期操作取消".to_string());
            }
            let port = find_available_port()?;
            let child = match spawn_backend(&BackendLaunchConfig {
                launcher: backend_runtime.launcher(),
                backend_dir: backend_runtime.backend_dir(),
                port,
                bootstate_file: &bootstate,
                uv_cache_dir: backend_runtime.uv_cache_dir(),
                terminal_worker: backend_runtime.terminal_worker(),
                log_file: &log_file,
                structured_log_dir: &runtime_dir,
            }) {
                Ok(child) => child,
                Err(error) => {
                    last_error = error;
                    if self.inner.shutting_down.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    if self.inner.lifecycle_generation.load(Ordering::Acquire) != generation {
                        return Err("后端启动已被新的生命周期操作取消".to_string());
                    }
                    continue;
                }
            };
            let mut child_slot = self
                .inner
                .child
                .lock()
                .map_err(|_| "后端进程锁已损坏".to_string())?;
            if child_slot.is_some()
                || self.inner.shutting_down.load(Ordering::Acquire)
                || self.inner.lifecycle_generation.load(Ordering::Acquire) != generation
            {
                drop(child_slot);
                let mut child = child;
                terminate_process_tree(&mut child);
                if self.inner.shutting_down.load(Ordering::Acquire) {
                    return Ok(());
                }
                return Err("后端启动已被新的生命周期操作取消".to_string());
            }
            *child_slot = Some(ManagedBackendProcess {
                generation,
                process: child,
            });
            drop(child_slot);
            match wait_for_backend(HOST, port, READY_TIMEOUT, &bootstate, || {
                self.inner
                    .child
                    .lock()
                    .ok()
                    .and_then(|mut child| {
                        child
                            .as_mut()
                            .map(|value| value.process.child.try_wait().ok().flatten().is_none())
                    })
                    .unwrap_or(false)
            }) {
                Ok(()) => {
                    let _lifecycle_guard = self
                        .inner
                        .lifecycle_gate
                        .lock()
                        .map_err(|_| "后端生命周期锁已损坏".to_string())?;
                    if self.inner.shutting_down.load(Ordering::Acquire)
                        || self.inner.lifecycle_generation.load(Ordering::Acquire) != generation
                    {
                        return Ok(());
                    }
                    *self
                        .inner
                        .backend_base_url
                        .lock()
                        .map_err(|_| "后端地址锁已损坏".to_string())? =
                        Some(format!("http://{HOST}:{port}"));
                    self.set_status(BackendStatus::Ready);
                    self.spawn_monitor(app.clone(), generation);
                    self.log("backend_ready");
                    return Ok(());
                }
                Err(error) => {
                    last_error = error;
                    let Some(()) = self.retry_after_health_failure(generation) else {
                        return Ok(());
                    };
                }
            }
        }
        Err(last_error)
    }

    pub fn stop(&self) {
        let Ok(_lifecycle_guard) = self.inner.lifecycle_gate.lock() else {
            return;
        };
        self.stop_locked();
    }

    fn stop_locked(&self) {
        self.inner.shutting_down.store(true, Ordering::Release);
        let child = self
            .inner
            .child
            .lock()
            .ok()
            .and_then(|mut value| value.take());
        self.inner
            .lifecycle_generation
            .fetch_add(1, Ordering::AcqRel);
        if let Some(mut child) = child {
            self.set_status(BackendStatus::Starting);
            terminate_process_tree(&mut child.process);
        }
        if let Ok(mut base_url) = self.inner.backend_base_url.lock() {
            *base_url = None;
        }
        self.set_status(BackendStatus::Stopped);
        self.log("backend_stopped");
    }

    pub fn prepare_start(&self) -> usize {
        self.inner.shutting_down.store(false, Ordering::Release);
        let generation = self
            .inner
            .lifecycle_generation
            .fetch_add(1, Ordering::AcqRel)
            + 1;
        self.set_status(BackendStatus::Starting);
        generation
    }

    pub fn reset_crash_recovery_budget(&self) {
        self.inner
            .crash_recovery_attempts
            .store(0, Ordering::Release);
    }

    pub fn status(&self) -> BackendStatus {
        self.inner
            .status
            .lock()
            .map(|status| status.clone())
            .unwrap_or(BackendStatus::Failed {
                message: "无法读取后端状态".to_string(),
            })
    }

    pub fn backend_base_url(&self) -> Option<String> {
        self.inner
            .backend_base_url
            .lock()
            .ok()
            .and_then(|value| value.clone())
    }
    fn fail(&self, message: String) -> Result<(), String> {
        self.set_status(BackendStatus::Failed {
            message: message.clone(),
        });
        self.log("backend_failed");
        Err(message)
    }

    fn retry_after_health_failure(&self, generation: usize) -> Option<()> {
        let Ok(_lifecycle_guard) = self.inner.lifecycle_gate.lock() else {
            return None;
        };
        if self.inner.shutting_down.load(Ordering::Acquire)
            || !is_current_generation(
                self.inner.lifecycle_generation.load(Ordering::Acquire),
                generation,
            )
        {
            return None;
        }
        let child = self
            .inner
            .child
            .lock()
            .ok()
            .and_then(|mut value| value.take());
        if let Some(mut child) = child {
            terminate_process_tree(&mut child.process);
        }
        if let Ok(mut base_url) = self.inner.backend_base_url.lock() {
            *base_url = None;
        }
        self.set_status(BackendStatus::Starting);
        // generation 的推进统一由下一轮 prepare_start 完成，避免一次重试
        // 同时在这里和循环入口递增两次，导致局部 generation 失配。
        Some(())
    }

    pub fn fail_for_startup(&self, message: String) -> Result<(), String> {
        self.fail(message)
    }
    fn set_status(&self, status: BackendStatus) {
        if let Ok(mut current) = self.inner.status.lock() {
            *current = status;
        }
    }

    fn set_status_if_generation(&self, generation: usize, status: BackendStatus) {
        let Ok(_lifecycle_guard) = self.inner.lifecycle_gate.lock() else {
            return;
        };
        if is_current_generation(
            self.inner.lifecycle_generation.load(Ordering::Acquire),
            generation,
        ) && !self.inner.shutting_down.load(Ordering::Acquire)
        {
            self.set_status(status);
        }
    }
    fn spawn_monitor(&self, app: AppHandle, generation: usize) {
        let supervisor = self.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            if !is_current_generation(
                supervisor
                    .inner
                    .lifecycle_generation
                    .load(Ordering::Acquire),
                generation,
            ) {
                break;
            }
            let exited = supervisor
                .inner
                .child
                .lock()
                .ok()
                .and_then(|mut child| {
                    child.as_mut().and_then(|value| {
                        if value.generation != generation {
                            None
                        } else {
                            value.process.child.try_wait().ok()
                        }
                    })
                })
                .flatten();
            if exited.is_some() {
                let removed = supervisor.inner.child.lock().ok().and_then(|mut child| {
                    if child
                        .as_ref()
                        .is_some_and(|value| value.generation == generation)
                    {
                        child.take()
                    } else {
                        None
                    }
                });
                if let Some(mut child) = removed {
                    terminate_process_tree(&mut child.process);
                } else {
                    break;
                }
                let Ok(_lifecycle_guard) = supervisor.inner.lifecycle_gate.lock() else {
                    break;
                };
                if !is_current_generation(
                    supervisor
                        .inner
                        .lifecycle_generation
                        .load(Ordering::Acquire),
                    generation,
                ) {
                    break;
                }
                if let Ok(mut base_url) = supervisor.inner.backend_base_url.lock() {
                    *base_url = None;
                }
                supervisor.log("backend_exited");

                if supervisor.inner.shutting_down.load(Ordering::Acquire) {
                    supervisor.set_status(BackendStatus::Stopped);
                    break;
                }

                // 只允许一次进程生命周期内的自动恢复；成功恢复后不重置预算，
                // 后续再次崩溃进入人工重试，避免把无限重启伪装成可靠性机制。
                let attempt = supervisor
                    .inner
                    .crash_recovery_attempts
                    .fetch_add(1, Ordering::AcqRel);
                if should_attempt_crash_recovery(attempt) {
                    drop(_lifecycle_guard);
                    let next_generation = supervisor.begin_recovery(generation);
                    if next_generation.is_some() && supervisor.start(&app).is_ok() {
                        break;
                    }
                } else {
                    drop(_lifecycle_guard);
                }
                supervisor.set_status_if_generation(
                    generation,
                    BackendStatus::Failed {
                        message: "本地 Agent 后端已退出，请点击重试".to_string(),
                    },
                );
                break;
            }
            if supervisor
                .inner
                .child
                .lock()
                .map(|child| child.is_none())
                .unwrap_or(true)
            {
                break;
            }
        });
    }

    fn begin_recovery(&self, generation: usize) -> Option<usize> {
        let Ok(_lifecycle_guard) = self.inner.lifecycle_gate.lock() else {
            return None;
        };
        if self.inner.shutting_down.load(Ordering::Acquire)
            || !is_current_generation(
                self.inner.lifecycle_generation.load(Ordering::Acquire),
                generation,
            )
        {
            return None;
        }
        self.inner
            .lifecycle_generation
            .fetch_add(1, Ordering::AcqRel);
        self.set_status(BackendStatus::Starting);
        Some(generation + 1)
    }
    fn log(&self, message: &str) {
        let Ok(path) = self.inner.log_file.lock() else {
            return;
        };
        let Some(path) = path.as_ref() else { return };
        let _ = append_json_line(
            path,
            serde_json::json!({
                "level": "INFO",
                "logger": "coding_agent.desktop",
                "trace_id": "",
                "caller": "backend_supervisor",
                "event": message,
                "msg": message,
                "data": {},
                "error": null,
                "truncated": false
            }),
        );
    }

    pub fn show_main_window(&self, app: &AppHandle) {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

struct ManagedBackendProcess {
    generation: usize,
    process: BackendProcess,
}

struct StartGuard<'a>(&'a AtomicBool);

impl Drop for StartGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[tauri::command]
pub fn backend_status(supervisor: State<'_, BackendSupervisor>) -> BackendStatus {
    supervisor.status()
}

#[tauri::command]
pub fn backend_runtime_config(
    supervisor: State<'_, BackendSupervisor>,
) -> Result<BackendRuntimeConfig, String> {
    let backend_base_url = supervisor
        .backend_base_url()
        .ok_or_else(|| "本地 Agent 后端尚未就绪".to_string())?;
    Ok(BackendRuntimeConfig {
        backend_base_url,
        status: supervisor.status(),
    })
}

#[tauri::command]
pub fn restart_backend(
    app: AppHandle,
    supervisor: State<'_, BackendSupervisor>,
) -> Result<BackendRuntimeConfig, String> {
    let lifecycle_guard = supervisor
        .inner
        .lifecycle_gate
        .lock()
        .map_err(|_| "后端生命周期锁已损坏".to_string())?;
    if supervisor.inner.start_in_progress.load(Ordering::Acquire) {
        return Err("本地 Agent 后端正在启动，请等待启动完成".to_string());
    }
    supervisor.stop_locked();
    supervisor.reset_crash_recovery_budget();
    supervisor.prepare_start();
    drop(lifecycle_guard);
    supervisor.start(&app)?;
    let backend_base_url = supervisor
        .backend_base_url()
        .ok_or_else(|| "本地 Agent 后端重启后未提供地址".to_string())?;
    Ok(BackendRuntimeConfig {
        backend_base_url,
        status: supervisor.status(),
    })
}

#[tauri::command]
pub fn write_frontend_log(app: AppHandle, entry: FrontendLogEntry) -> Result<(), String> {
    if entry.level != "DEBUG"
        && entry.level != "INFO"
        && entry.level != "WARNING"
        && entry.level != "ERROR"
    {
        return Err("日志 level 不符合统一契约".to_string());
    }
    if entry.trace_id.len() != 32
        || !entry
            .trace_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("日志 trace_id 不符合统一契约".to_string());
    }
    if time::OffsetDateTime::parse(&entry.ts, &time::format_description::well_known::Rfc3339)
        .is_err()
        || !is_snake_case(&entry.event)
        || !is_log_name(&entry.logger)
        || !is_log_name(&entry.caller)
        || entry.event.is_empty()
        || entry.msg.is_empty()
        || !entry.data.is_object()
    {
        return Err("日志字段不符合统一契约".to_string());
    }
    let path = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("无法解析 Cosir 数据目录：{error}"))?
        .join("runtime")
        .join("frontend.log");
    append_json_line(
        &path,
        serde_json::to_value(entry).map_err(|error| format!("日志序列化失败：{error}"))?,
    )
}

fn is_snake_case(value: &str) -> bool {
    !value.is_empty()
        && value.as_bytes()[0].is_ascii_lowercase()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
        && !value.starts_with('_')
        && !value.ends_with('_')
}

fn is_log_name(value: &str) -> bool {
    !value.is_empty() && value.split('.').all(is_snake_case)
}

fn find_available_port() -> Result<u16, String> {
    std::net::TcpListener::bind((HOST, 0))
        .and_then(|listener| listener.local_addr())
        .map(|address| address.port())
        .map_err(|error| format!("无法分配本地后端端口：{error}"))
}

#[cfg(test)]
mod tests {
    use super::{should_attempt_crash_recovery, MAX_CRASH_RECOVERY_ATTEMPTS};
    use std::sync::atomic::Ordering;

    #[test]
    fn crash_recovery_is_finite_and_does_not_loop_after_budget() {
        assert!(should_attempt_crash_recovery(0));
        assert!(!should_attempt_crash_recovery(MAX_CRASH_RECOVERY_ATTEMPTS));
        assert!(!should_attempt_crash_recovery(usize::MAX));
    }

    #[test]
    fn monitor_generation_comparison_rejects_stale_generation() {
        assert!(super::is_current_generation(7, 7));
        assert!(!super::is_current_generation(8, 7));
    }

    #[test]
    fn health_failure_retry_advances_generation_once_on_next_start() {
        let supervisor = super::BackendSupervisor::default();
        assert_eq!(supervisor.prepare_start(), 1);
        assert!(supervisor.retry_after_health_failure(1).is_some());
        assert_eq!(
            supervisor
                .inner
                .lifecycle_generation
                .load(Ordering::Acquire),
            1
        );
        assert_eq!(supervisor.prepare_start(), 2);
    }

    #[test]
    fn health_failure_retry_does_not_reopen_shutdown_lifecycle() {
        let supervisor = super::BackendSupervisor::default();
        assert_eq!(supervisor.prepare_start(), 1);
        supervisor.stop();
        assert!(supervisor.retry_after_health_failure(1).is_none());
        assert!(supervisor.inner.shutting_down.load(Ordering::Acquire));
        assert_eq!(
            supervisor
                .inner
                .lifecycle_generation
                .load(Ordering::Acquire),
            2
        );
    }
}
