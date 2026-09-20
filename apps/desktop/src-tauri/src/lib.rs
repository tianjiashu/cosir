mod backend_process;
mod backend_readiness;
mod backend_runtime;
mod backend_supervisor;
mod data_paths;
mod desktop_log;
mod file_access;
mod log_paths;
mod tray;
#[cfg(windows)]
mod webview_diagnostics;

use backend_supervisor::{
    backend_runtime_config, backend_status, restart_backend, write_frontend_log, BackendSupervisor,
};
use tauri::{Manager, RunEvent, WindowEvent};

/// 通过 Tauri 原生关闭接口触发主窗口关闭，仅供桌面 E2E 验证使用。
#[cfg(feature = "desktop-e2e")]
#[tauri::command]
fn e2e_close_main_window(app: tauri::AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "主窗口不存在".to_string())?;
    window.close().map_err(|error| error.to_string())
}

/// 查询主窗口可见性，仅供桌面 E2E 验证使用。
#[cfg(feature = "desktop-e2e")]
#[tauri::command]
fn e2e_main_window_is_visible(app: tauri::AppHandle) -> Result<bool, String> {
    app.get_webview_window("main")
        .ok_or_else(|| "主窗口不存在".to_string())?
        .is_visible()
        .map_err(|error| error.to_string())
}

pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            app.state::<BackendSupervisor>().show_main_window(app);
        }))
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let supervisor = window.app_handle().state::<BackendSupervisor>();
                match window.hide() {
                    Ok(()) => supervisor.log_lifecycle_event("main_window_hidden_to_tray"),
                    Err(_) => supervisor.log_lifecycle_event("main_window_hide_failed"),
                }
            }
        });

    // The embedded WebDriver server is a debug-only test boundary. It is
    // enabled only by the `desktop-e2e` feature and is never part of the
    // packaged production application.
    #[cfg(feature = "desktop-e2e")]
    let builder = builder
        .plugin(tauri_plugin_wdio::init())
        .plugin(tauri_plugin_wdio_webdriver::init());

    let builder = builder.manage(BackendSupervisor::default());

    #[cfg(feature = "desktop-e2e")]
    let builder = builder.invoke_handler(tauri::generate_handler![
        backend_status,
        backend_runtime_config,
        restart_backend,
        write_frontend_log,
        file_access::read_selected_attachment_file,
        file_access::resolve_selected_attachment_path,
        e2e_close_main_window,
        e2e_main_window_is_visible,
    ]);

    #[cfg(not(feature = "desktop-e2e"))]
    let builder = builder.invoke_handler(tauri::generate_handler![
        backend_status,
        backend_runtime_config,
        restart_backend,
        write_frontend_log,
        file_access::read_selected_attachment_file,
        file_access::resolve_selected_attachment_path,
    ]);

    builder
        .setup(|app| {
            let supervisor = app.state::<BackendSupervisor>().inner().clone();
            let handle = app.handle().clone();
            tray::install(app)?;
            #[cfg(windows)]
            if let Err(error) = webview_diagnostics::install(app.handle()) {
                if let Ok(log_dir) = log_paths::app_log_dir(app.handle()) {
                    let _ = desktop_log::append_json_line(
                        &log_dir.join("desktop.log"),
                        serde_json::json!({
                            "level": "WARNING",
                            "logger": "coding_agent.desktop",
                            "trace_id": "",
                            "caller": "webview_diagnostics",
                            "event": "webview_diagnostics_install_failed",
                            "msg": "webview_diagnostics_install_failed",
                            "data": {},
                            "error": {"message": error},
                            "truncated": false
                        }),
                    );
                }
            }
            supervisor.prepare_start();
            // 窗口生命周期不依赖后端健康检查；前端负责展示 starting/failed/ready 状态。
            supervisor.show_main_window(app.handle());
            std::thread::spawn(move || {
                if let Err(error) = supervisor.start(&handle) {
                    let _ = supervisor.fail_for_startup(error);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Cosir Tauri 应用装配失败")
        .run(|app, event| match event {
            RunEvent::Exit => app.state::<BackendSupervisor>().stop(),
            #[cfg(target_os = "macos")]
            RunEvent::Reopen {
                has_visible_windows: false,
                ..
            } => app.state::<BackendSupervisor>().show_main_window(app),
            _ => {}
        });
}
