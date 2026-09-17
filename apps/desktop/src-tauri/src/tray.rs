use tauri::{
    menu::MenuBuilder,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, Manager,
};

use crate::backend_supervisor::BackendSupervisor;

const SHOW_MENU_ID: &str = "show_main_window";
const QUIT_MENU_ID: &str = "quit_and_stop_backend";

/// 安装常驻托盘及其显式显示、退出入口。
///
/// 关闭主窗口只会隐藏窗口；托盘退出会请求应用退出，随后由
/// `BackendSupervisor` 在 Tauri 的退出事件中清理后端进程。菜单名称明确说明退出会停止运行。
///
/// 参数:
///     app: 已装配 ``BackendSupervisor`` 的 Tauri 应用。
///
/// 返回:
///     菜单或托盘图标创建成功时返回 ``Ok(())``。
///
/// 异常:
///     菜单项或托盘图标创建失败时返回 Tauri 错误，由宿主启动边界报告。
///
/// 副作用:
///     创建原生托盘菜单与图标，并将图标句柄登记到应用状态以保持其生命周期。
pub fn install(app: &mut App) -> tauri::Result<()> {
    let menu = MenuBuilder::new(app)
        .text(SHOW_MENU_ID, "显示 Cosir")
        .separator()
        .text(QUIT_MENU_ID, "退出并停止运行")
        .build()?;

    let tray = TrayIconBuilder::new()
        .icon(tauri::include_image!("./icons/icon.ico"))
        // Windows/macOS 左键直接显示窗口；菜单中的“显示 Cosir”始终保留，
        // 不依赖平台是否发出托盘点击事件。
        .show_menu_on_left_click(false)
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            SHOW_MENU_ID => app.state::<BackendSupervisor>().show_main_window(app),
            QUIT_MENU_ID => {
                app.state::<BackendSupervisor>()
                    .log_lifecycle_event("application_exit_requested_from_tray");
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                let app = tray.app_handle();
                app.state::<BackendSupervisor>().show_main_window(app);
            }
        })
        .build(app)?;

    // Keep the native icon handle alive for the full application lifetime.
    let _ = app.manage(tray);
    Ok(())
}
