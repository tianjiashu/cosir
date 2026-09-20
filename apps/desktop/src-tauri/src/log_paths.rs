use std::path::PathBuf;

use crate::data_paths::system_cosir_dir;
use tauri::AppHandle;

/// Resolve the single OS-managed directory used by all desktop diagnostics.
pub fn app_log_dir(app: &AppHandle) -> Result<PathBuf, String> {
    system_cosir_dir(app).map(|path| path.join("logs"))
}
