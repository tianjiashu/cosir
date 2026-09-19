use std::path::Path;

use tauri::{AppHandle, Manager};
use webview2_com::Microsoft::Web::WebView2::Win32::{
    ICoreWebView2_2, COREWEBVIEW2_PROCESS_FAILED_KIND, COREWEBVIEW2_WEB_ERROR_STATUS,
};
use webview2_com::{
    ContentLoadingEventHandler, DOMContentLoadedEventHandler, NavigationCompletedEventHandler,
    ProcessFailedEventHandler,
};
use windows::core::{Interface, BOOL};

use crate::desktop_log::append_json_line;
use crate::log_paths::app_log_dir;

/// Attach low-level WebView2 lifecycle diagnostics without changing page behavior.
pub fn install(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "主 WebView 不存在".to_string())?;
    let log_path = app_log_dir(app)?.join("desktop.log");

    window
        .with_webview(move |webview| {
            let process_failed_log = log_path.clone();
            let process_failed =
                ProcessFailedEventHandler::create(Box::new(move |_sender, args| {
                    let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND(0);
                    if let Some(args) = args {
                        unsafe {
                            let _ = args.ProcessFailedKind(&mut kind);
                        }
                    }
                    write_event(
                        &process_failed_log,
                        "WARNING",
                        "webview2_process_failed",
                        serde_json::json!({
                            "kind": kind.0,
                            "kind_name": process_failed_kind_name(kind),
                        }),
                    );
                    Ok(())
                }));

            let content_loading_log = log_path.clone();
            let content_loading =
                ContentLoadingEventHandler::create(Box::new(move |_sender, _args| {
                    write_event(
                        &content_loading_log,
                        "INFO",
                        "webview_content_loading",
                        serde_json::json!({}),
                    );
                    Ok(())
                }));

            let dom_loaded_log = log_path.clone();
            let dom_loaded =
                DOMContentLoadedEventHandler::create(Box::new(move |_sender, _args| {
                    write_event(
                        &dom_loaded_log,
                        "INFO",
                        "webview_dom_content_loaded",
                        serde_json::json!({}),
                    );
                    Ok(())
                }));

            let navigation_log = log_path.clone();
            let navigation_completed =
                NavigationCompletedEventHandler::create(Box::new(move |_sender, args| {
                    let mut success = BOOL(0);
                    let mut error_status = COREWEBVIEW2_WEB_ERROR_STATUS(0);
                    if let Some(args) = args {
                        unsafe {
                            let _ = args.IsSuccess(&mut success);
                            let _ = args.WebErrorStatus(&mut error_status);
                        }
                    }
                    write_event(
                        &navigation_log,
                        if success.as_bool() { "INFO" } else { "WARNING" },
                        "webview_navigation_completed",
                        serde_json::json!({
                            "success": success.as_bool(),
                            "error_status": error_status.0,
                        }),
                    );
                    Ok(())
                }));

            let webview = unsafe { webview.controller().CoreWebView2() };
            let Ok(webview) = webview else {
                write_event(
                    &log_path,
                    "WARNING",
                    "webview2_handle_unavailable",
                    serde_json::json!({}),
                );
                return;
            };
            let webview2 = webview.cast::<ICoreWebView2_2>().ok();
            unsafe {
                let mut process_failed_token = 0;
                let mut content_loading_token = 0;
                let mut dom_loaded_token = 0;
                let mut navigation_completed_token = 0;
                if let Err(error) =
                    webview.add_ProcessFailed(&process_failed, &mut process_failed_token)
                {
                    write_registration_failure(&log_path, "ProcessFailed", error);
                }
                if let Err(error) =
                    webview.add_ContentLoading(&content_loading, &mut content_loading_token)
                {
                    write_registration_failure(&log_path, "ContentLoading", error);
                }
                if let Some(webview2) = webview2 {
                    if let Err(error) =
                        webview2.add_DOMContentLoaded(&dom_loaded, &mut dom_loaded_token)
                    {
                        write_registration_failure(&log_path, "DOMContentLoaded", error);
                    }
                } else {
                    write_event(
                        &log_path,
                        "WARNING",
                        "webview2_versioned_handle_unavailable",
                        serde_json::json!({"interface": "ICoreWebView2_2"}),
                    );
                }
                if let Err(error) = webview
                    .add_NavigationCompleted(&navigation_completed, &mut navigation_completed_token)
                {
                    write_registration_failure(&log_path, "NavigationCompleted", error);
                }
            }
        })
        .map_err(|error| format!("无法注册 WebView2 诊断事件：{error}"))
}

fn write_registration_failure(path: &Path, handler: &str, error: windows::core::Error) {
    write_event(
        path,
        "WARNING",
        "webview2_diagnostics_handler_install_failed",
        serde_json::json!({
            "handler": handler,
            "error": error.to_string(),
        }),
    );
}

fn write_event(path: &Path, level: &str, event: &str, data: serde_json::Value) {
    let _ = append_json_line(
        path,
        serde_json::json!({
            "level": level,
            "logger": "coding_agent.desktop",
            "trace_id": "",
            "caller": "webview_diagnostics",
            "event": event,
            "msg": event,
            "data": data,
            "error": null,
            "truncated": false
        }),
    );
}

fn process_failed_kind_name(kind: COREWEBVIEW2_PROCESS_FAILED_KIND) -> &'static str {
    match kind.0 {
        0 => "browser_process_exited",
        1 => "render_process_exited",
        2 => "render_process_unresponsive",
        3 => "frame_render_process_exited",
        4 => "utility_process_exited",
        5 => "sandbox_helper_process_exited",
        6 => "gpu_process_exited",
        7 => "ppapi_plugin_process_exited",
        8 => "ppapi_broker_process_exited",
        9 => "unknown_process_exited",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::process_failed_kind_name;
    use webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_PROCESS_FAILED_KIND;

    #[test]
    fn names_renderer_process_failures() {
        assert_eq!(
            process_failed_kind_name(COREWEBVIEW2_PROCESS_FAILED_KIND(1)),
            "render_process_exited"
        );
        assert_eq!(
            process_failed_kind_name(COREWEBVIEW2_PROCESS_FAILED_KIND(999)),
            "unknown"
        );
    }
}
