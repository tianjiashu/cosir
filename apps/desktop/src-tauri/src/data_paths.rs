use std::path::PathBuf;

use tauri::{AppHandle, Manager};

const SYSTEM_COSIR_DIR_NAME: &str = ".cosir";

/// 解析系统级 Cosir 数据根目录。
///
/// macOS 与 Windows 使用当前用户主目录，其他桌面平台沿用 Tauri 的应用数据目录。
/// 调用方应把返回值作为 `CODING_AGENT_DATA_DIR` 注入后端，由后端统一追加 `.cosir`。
pub fn system_data_root(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(any(target_os = "macos", windows))]
    {
        return app
            .path()
            .home_dir()
            .map_err(|error| format!("无法解析 Cosir 用户主目录：{error}"));
    }

    #[cfg(not(any(target_os = "macos", windows)))]
    {
        app.path()
            .app_data_dir()
            .map_err(|error| format!("无法解析 Cosir 应用数据目录：{error}"))
    }
}

/// 解析系统级 `.cosir` 目录。
pub fn system_cosir_dir(app: &AppHandle) -> Result<PathBuf, String> {
    system_data_root(app).map(|path| path.join(SYSTEM_COSIR_DIR_NAME))
}
