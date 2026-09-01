mod backend_process;
mod backend_readiness;
mod backend_supervisor;

use backend_supervisor::{backend_status, restart_backend, BackendSupervisor};
use tauri::{Manager, RunEvent};

pub fn run() {
    tauri::Builder::default()
        .manage(BackendSupervisor::default())
        .invoke_handler(tauri::generate_handler![backend_status, restart_backend])
        .setup(|app| {
            let supervisor = app.state::<BackendSupervisor>().inner().clone();
            let handle = app.handle().clone();
            supervisor.prepare_start();
            std::thread::spawn(move || {
                if let Err(error) = supervisor.start(&handle) {
                    eprintln!("[cosir] 本地 Agent 后端启动失败：{error}");
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
