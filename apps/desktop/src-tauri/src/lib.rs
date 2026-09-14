mod backend_process;
mod backend_readiness;
mod backend_runtime;
mod backend_supervisor;
mod desktop_log;
mod file_access;
#[cfg(windows)]
mod webview_diagnostics;

use backend_supervisor::{
    backend_runtime_config, backend_status, restart_backend, write_frontend_log, BackendSupervisor,
};
use tauri::{Manager, RunEvent};

pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            app.state::<BackendSupervisor>().show_main_window(app);
        }));

    // The embedded WebDriver server is a debug-only test boundary. It is
    // enabled only by the `desktop-e2e` feature and is never part of the
    // packaged production application.
    #[cfg(feature = "desktop-e2e")]
    let builder = builder
        .plugin(tauri_plugin_wdio::init())
        .plugin(tauri_plugin_wdio_webdriver::init());

    builder
        .manage(BackendSupervisor::default())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            backend_runtime_config,
            restart_backend,
            write_frontend_log,
            file_access::read_selected_attachment_file,
            file_access::resolve_selected_attachment_path,
        ])
        .setup(|app| {
            let supervisor = app.state::<BackendSupervisor>().inner().clone();
            let handle = app.handle().clone();
            #[cfg(windows)]
            if let Err(error) = webview_diagnostics::install(app.handle()) {
                let _ = desktop_log::append_json_line(
                    &app.path()
                        .app_data_dir()
                        .map(|path| path.join("runtime").join("desktop.log"))
                        .unwrap_or_else(|_| std::path::PathBuf::from("desktop.log")),
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
        .run(|app, event| {
            if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
                app.state::<BackendSupervisor>().stop();
            }
        });
}
