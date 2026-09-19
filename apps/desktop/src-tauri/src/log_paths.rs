use std::path::PathBuf;

use tauri::{AppHandle, Manager};

/// Resolve the single OS-managed directory used by all desktop diagnostics.
pub fn app_log_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|path| path.join("logs"))
        .map_err(|error| format!("无法解析 Cosir 日志目录：{error}"))
}
