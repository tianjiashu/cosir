//! Tauri 2 应用库入口。
//!
//! 承载窗口初始化、IPC 命令注册、插件装配与本地后端托管状态注入。
//! 桌面端由 `main.rs` 的 `main()` 调用；移动端入口点通过
//! `mobile_entry_point` 预留。

mod backend;
mod commands;

use tauri::Manager;

/// 应用入口逻辑。
///
/// 初始化 Tauri Builder、注册插件与 IPC 命令、创建主窗口。
/// 桌面端由二进制入口 `main()` 调用；移动端由 `mobile_entry_point` 调用。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(backend::supervisor::BackendSupervisorState::new())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            commands::logging::log_write,
            commands::backend::backend_start,
            commands::backend::backend_stop,
            commands::backend::backend_restart,
            commands::backend::backend_status,
            commands::backend::backend_logs_tail,
            commands::fs::open_file_in_editor,
        ])
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("无法获取主窗口 'main'，请检查 tauri.conf.json 的窗口配置");
            eprintln!("Tauri 窗口已创建: {:?}", window.label());
            // 打印前端日志落盘路径，便于排查（避免在 src-tauri 内写入触发 dev 重建）
            match commands::logging::get_log_dir(app.handle()) {
                Ok(dir) => eprintln!("[log_write] 前端日志目录: {}", dir.join("desktop.log").display()),
                Err(e) => eprintln!("[log_write] 解析日志目录失败: {e}"),
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("运行 Tauri 应用时出错");
}
