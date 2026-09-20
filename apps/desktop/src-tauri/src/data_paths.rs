use std::path::PathBuf;

use tauri::{AppHandle, Manager};

const SYSTEM_COSIR_DIR_NAME: &str = ".cosir";

/// Resolve the single OS-managed root for Cosir runtime data.
pub fn system_cosir_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|path| path.join(SYSTEM_COSIR_DIR_NAME))
        .map_err(|error| format!("无法解析 Cosir 系统 .cosir 目录：{error}"))
}
