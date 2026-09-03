mod backend_process;
mod backend_readiness;
mod backend_supervisor;
mod desktop_log;

use backend_supervisor::{
    backend_runtime_config, backend_status, restart_backend, write_frontend_log, BackendSupervisor,
};
use tauri::{Manager, RunEvent};

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            app.state::<BackendSupervisor>()
                .show_main_window_if_settled(app);
        }))
        .manage(BackendSupervisor::default())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            backend_runtime_config,
            restart_backend,
            write_frontend_log,
        ])
        .setup(|app| {
            let supervisor = app.state::<BackendSupervisor>().inner().clone();
            let handle = app.handle().clone();
            supervisor.prepare_start();
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
