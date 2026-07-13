//! Tauri 2 应用库入口。
//!
//! 承载窗口初始化、IPC 命令注册与插件装配。桌面端由 `main.rs` 的
//! `main()` 调用；移动端入口点通过 `mobile_entry_point` 预留。
//!
//! # 已注册命令
//! - `greet`：示例问候（开发期验证 IPC 通道用）
//! - `log_write`：前端统一日志落盘（写入应用日志目录下的 desktop.log）

mod commands;

use tauri::Manager;

/// 应用入口逻辑。
///
/// 初始化 Tauri Builder、注册插件与 IPC 命令、创建主窗口。
/// 桌面端由二进制入口 `main()` 调用；移动端由 `mobile_entry_point` 调用。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            commands::greet,
            commands::log_write,
        ])
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("无法获取主窗口 'main'，请检查 tauri.conf.json 的窗口配置");
            eprintln!("Tauri 窗口已创建: {:?}", window.label());
            // 打印前端日志落盘路径，便于排查（避免在 src-tauri 内写入触发 dev 重建）
            match commands::get_log_dir(app.handle()) {
                Ok(dir) => eprintln!("[log_write] 前端日志目录: {}", dir.display()),
                Err(e) => eprintln!("[log_write] 解析日志目录失败: {e}"),
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("运行 Tauri 应用时出错");
}
