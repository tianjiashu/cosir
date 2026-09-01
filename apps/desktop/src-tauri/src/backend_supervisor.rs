use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Manager, State};

use crate::backend_process::{spawn_backend, terminate_process_tree, BackendProcess};
use crate::backend_readiness::wait_for_backend;

const HOST: &str = "127.0.0.1";
const PORT: u16 = 8000;
const READY_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Clone, Serialize)]
#[serde(rename_all = "snake_case", tag = "state")]
pub enum BackendStatus {
    Starting,
    Ready,
    Failed { message: String },
    Stopped,
}

struct BackendInner {
    child: Mutex<Option<BackendProcess>>,
    shutting_down: AtomicBool,
    status: Mutex<BackendStatus>,
    log_file: Mutex<Option<PathBuf>>,
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
            }),
        }
    }
}

impl BackendSupervisor {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        match self.start_inner(app) {
            Ok(()) => Ok(()),
            Err(error) => self.fail(error),
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
        let _ = std::fs::remove_file(&bootstate);
        self.set_status(BackendStatus::Starting);
        if std::net::TcpListener::bind((HOST, PORT)).is_err() {
            return Err("开发后端端口 8000 已被占用".to_string());
        }
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
        let log_file = runtime_dir.join("backend.log");
        let mut child_slot = self
            .inner
            .child
            .lock()
            .map_err(|_| "后端进程锁已损坏".to_string())?;
        if child_slot.is_some() {
            return Ok(());
        }
        if self.inner.shutting_down.load(Ordering::Acquire) {
            return Ok(());
        }
        let child = spawn_backend(&launcher, &backend_dir, PORT, &bootstate, use_uv, &log_file)?;
        *child_slot = Some(child);
        drop(child_slot);
        if let Err(error) = wait_for_backend(HOST, PORT, READY_TIMEOUT, &bootstate) {
            self.stop();
            return Err(error);
        }
        self.set_status(BackendStatus::Ready);
        self.spawn_monitor();
        self.log("backend_ready");
        Ok(())
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
        self.set_status(BackendStatus::Stopped);
        self.log("backend_stopped");
    }

    pub fn prepare_start(&self) {
        self.inner.shutting_down.store(false, Ordering::Release);
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
    fn fail(&self, message: String) -> Result<(), String> {
        self.set_status(BackendStatus::Failed {
            message: message.clone(),
        });
        self.log(&format!("backend_failed: {message}"));
        Err(message)
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
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            use std::io::Write;
            let _ = writeln!(file, "{} {}", timestamp(), message);
        }
    }
}

fn timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0)
}

#[tauri::command]
pub fn backend_status(supervisor: State<'_, BackendSupervisor>) -> BackendStatus {
    supervisor.status()
}

#[tauri::command]
pub fn restart_backend(
    app: AppHandle,
    supervisor: State<'_, BackendSupervisor>,
) -> Result<BackendStatus, String> {
    supervisor.stop();
    supervisor.prepare_start();
    supervisor.start(&app)?;
    Ok(supervisor.status())
}
