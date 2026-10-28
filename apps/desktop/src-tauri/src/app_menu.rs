use tauri::{
    menu::{AboutMetadata, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu},
    AppHandle, Manager, Wry,
};

use crate::backend_supervisor::BackendSupervisor;

const QUIT_MENU_ID: &str = "app_menu_quit";

/// 构建 macOS 应用菜单：沿用 Tauri 默认菜单，只把应用子菜单的退出项换成自定义项。
///
/// 默认的原生「Quit」直接调用 `NSApp terminate:`，tao 不会为它发出 `ExitRequested`，
/// 退出确认就拦不住 Cmd+Q。自定义项（同样绑定 Cmd+Q）改走 `app.exit`，与托盘退出共用
/// `ExitRequested` 一条路径。其余子菜单（Edit 的复制粘贴等快捷键依赖它）保持默认。
///
/// 前提：macOS 默认菜单的第一项是应用子菜单（Tauri `Menu::default` 的固定结构）。
pub fn build(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let menu = Menu::default(app)?;
    let name = app.package_info().name.clone();
    let about = AboutMetadata {
        name: Some(name.clone()),
        version: Some(app.package_info().version.to_string()),
        ..Default::default()
    };
    let app_submenu = Submenu::with_items(
        app,
        &name,
        true,
        &[
            &PredefinedMenuItem::about(app, None, Some(about))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::services(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(
                app,
                QUIT_MENU_ID,
                format!("退出 {name}"),
                true,
                Some("CmdOrCtrl+Q"),
            )?,
        ],
    )?;
    menu.remove_at(0)?;
    menu.prepend(&app_submenu)?;
    Ok(menu)
}

/// 处理应用菜单事件；只响应自定义退出项。
pub fn handle_event(app: &AppHandle, event: &MenuEvent) {
    if event.id().as_ref() == QUIT_MENU_ID {
        app.state::<BackendSupervisor>()
            .log_lifecycle_event("application_exit_requested_from_app_menu");
        app.exit(0);
    }
}
