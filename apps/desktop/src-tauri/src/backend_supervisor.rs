use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::backend_process::{spawn_backend, terminate_process_tree, BackendProcess};
use crate::backend_readiness::wait_for_backend;
use crate::desktop_log::append_json_line;

const HOST: &str = "127.0.0.1";
const READY_TIMEOUT: Duration = Duration::from_secs(90);

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
    child: Mutex<Option<BackendProcess>>,
    shutting_down: AtomicBool,
    status: Mutex<BackendStatus>,
    log_file: Mutex<Option<PathBuf>>,
    backend_base_url: Mutex<Option<String>>,
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
                shutting_down: AtomicBool::new(false),
                status: Mutex::new(BackendStatus::Stopped),
                log_file: Mutex::new(None),
                backend_base_url: Mutex::new(None),
            }),
        }
    }
}

impl BackendSupervisor {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        match self.start_inner(app) {
            Ok(()) => {
                self.show_main_window_if_settled(app);
                Ok(())
            }
            Err(error) => {
                let result = self.fail(error);
                self.show_main_window_if_settled(app);
                result
            }
        }
    }

    fn start_inner(&self, app: &AppHandle) -> Result<(), String> {
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
        let backend_dir = std::env::var_os("COSIR_BACKEND_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("..")
                    .join("..")
                    .join("backend")
            });
        let launcher = std::env::var_os("COSIR_BACKEND_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(if cfg!(debug_assertions) {
                    "uv"
                } else {
                    "python"
                })
            });
        let use_uv = std::env::var_os("COSIR_BACKEND_PYTHON").is_none() && cfg!(debug_assertions);
        let log_file = runtime_dir.join("backend-console.log");
        let mut last_error = "本地 Agent 后端启动失败".to_string();
        for attempt in 0..3 {
            if attempt > 0 {
                self.prepare_start();
                let _ = std::fs::remove_file(&bootstate);
            }
            if self.inner.shutting_down.load(Ordering::Acquire) {
                return Ok(());
            }
            let port = find_available_port()?;
            let child = match spawn_backend(
                &launcher,
                &backend_dir,
                port,
                &bootstate,
                use_uv,
                &log_file,
                &runtime_dir,
            ) {
                Ok(child) => child,
                Err(error) => {
                    last_error = error;
                    continue;
                }
            };
            let mut child_slot = self
                .inner
                .child
                .lock()
                .map_err(|_| "后端进程锁已损坏".to_string())?;
            if child_slot.is_some() {
                return Ok(());
            }
            *child_slot = Some(child);
            drop(child_slot);
            match wait_for_backend(HOST, port, READY_TIMEOUT, &bootstate, || {
                self.inner
                    .child
                    .lock()
                    .ok()
                    .and_then(|mut child| {
                        child
                            .as_mut()
                            .map(|value| value.child.try_wait().ok().flatten().is_none())
                    })
                    .unwrap_or(false)
            }) {
                Ok(()) => {
                    *self
                        .inner
                        .backend_base_url
                        .lock()
                        .map_err(|_| "后端地址锁已损坏".to_string())? =
                        Some(format!("http://{HOST}:{port}"));
                    self.set_status(BackendStatus::Ready);
                    self.spawn_monitor();
                    self.log("backend_ready");
                    return Ok(());
                }
                Err(error) => {
                    last_error = error;
                    self.stop();
                }
            }
        }
        Err(last_error)
    }

    pub fn stop(&self) {
        self.inner.shutting_down.store(true, Ordering::Release);
        let child = self
            .inner
            .child
            .lock()
            .ok()
            .and_then(|mut value| value.take());
        if let Some(mut child) = child {
            self.set_status(BackendStatus::Starting);
            terminate_process_tree(&mut child);
        }
        if let Ok(mut base_url) = self.inner.backend_base_url.lock() {
            *base_url = None;
        }
        self.set_status(BackendStatus::Stopped);
        self.log("backend_stopped");
    }

    pub fn prepare_start(&self) {
        self.inner.shutting_down.store(false, Ordering::Release);
        self.set_status(BackendStatus::Starting);
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

    pub fn fail_for_startup(&self, message: String) -> Result<(), String> {
        self.fail(message)
    }
    fn set_status(&self, status: BackendStatus) {
        if let Ok(mut current) = self.inner.status.lock() {
            *current = status;
        }
    }
    fn spawn_monitor(&self) {
        let supervisor = self.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            let exited = supervisor
                .inner
                .child
                .lock()
                .ok()
                .and_then(|mut child| child.as_mut().and_then(|value| value.child.try_wait().ok()))
                .flatten();
            if exited.is_some() {
                if let Ok(mut child) = supervisor.inner.child.lock() {
                    if let Some(mut child) = child.take() {
                        terminate_process_tree(&mut child);
                    }
                }
                supervisor.set_status(BackendStatus::Failed {
                    message: "本地 Agent 后端已退出，请点击重试".to_string(),
                });
                if let Ok(mut base_url) = supervisor.inner.backend_base_url.lock() {
                    *base_url = None;
                }
                supervisor.log("backend_exited");
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

    pub fn show_main_window_if_settled(&self, app: &AppHandle) {
        if matches!(self.status(), BackendStatus::Starting) {
            return;
        }
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.show();
        }
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
    supervisor.stop();
    supervisor.prepare_start();
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
