use std::sync::atomic::{AtomicU8, Ordering};
use std::time::Duration;

use tauri::{AppHandle, ExitRequestApi, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::backend_http;
use crate::backend_supervisor::BackendSupervisor;

const ACTIVE_COUNT_PATH: &str = "/terminal/sessions/active-count";
/// 查询只为提醒服务，宁可漏提醒也不能让退出卡住，故总时限很短。
const ACTIVE_COUNT_TIMEOUT: Duration = Duration::from_secs(1);
const LOG_CALLER: &str = "exit_confirmation";

const IDLE: u8 = 0;
const CONFIRMING: u8 = 1;
const CONFIRMED: u8 = 2;

/// 应用真正退出前的「仍有运行中终端会话」确认。
///
/// 只拦截 `RunEvent::ExitRequested`（托盘「退出并停止运行」、macOS 应用菜单 / Cmd+Q 的
/// 自定义退出项，二者都走 `app.exit`）；关闭窗口走 `CloseRequested` 隐藏到托盘，不经过这里。
/// macOS 原生 `terminate:`（Dock 退出、注销关机）不产生 `ExitRequested`，无法拦截，
/// 后端仍由 `RunEvent::Exit` 正常清理。
///
/// 事件回调运行在主线程且必须立即返回：首次请求先 `prevent_exit`，再在后台线程查询
/// 后端并弹异步原生对话框，确认后置为 `CONFIRMED` 并重新 `app.exit`，第二次
/// `ExitRequested` 直接放行。确认进行中重复的退出请求一律吞掉，避免叠加多个对话框。
/// 后端非 ready、查询失败或超时都直接退出，不阻塞用户。
#[derive(Default)]
pub struct ExitConfirmation {
    state: AtomicU8,
}

/// 处理一次 `ExitRequested`；必须在 Tauri 事件回调里同步调用。
pub fn handle_exit_requested(app: &AppHandle, code: Option<i32>, api: &ExitRequestApi) {
    let confirmation = app.state::<ExitConfirmation>();
    match confirmation
        .state
        .compare_exchange(IDLE, CONFIRMING, Ordering::AcqRel, Ordering::Acquire)
    {
        Ok(_) => {
            api.prevent_exit();
            let app = app.clone();
            std::thread::spawn(move || confirm_then_exit(app, code.unwrap_or(0)));
        }
        Err(CONFIRMING) => {
            api.prevent_exit();
            log(
                app,
                "INFO",
                "application_exit_confirmation_pending",
                serde_json::json!({}),
                None,
            );
        }
        // CONFIRMED：本次就是确认后的重新退出，放行。
        Err(_) => {}
    }
}

fn confirm_then_exit(app: AppHandle, exit_code: i32) {
    let Some(address) = app.state::<BackendSupervisor>().ready_backend_address() else {
        log(
            &app,
            "INFO",
            "application_exit_confirmation_skipped",
            serde_json::json!({"reason": "backend_not_ready"}),
            None,
        );
        return proceed_exit(&app, exit_code);
    };
    let count = match query_active_count(address) {
        Ok(count) => count,
        Err(error) => {
            log(
                &app,
                "WARNING",
                "terminal_active_count_query_failed",
                serde_json::json!({}),
                Some(error),
            );
            return proceed_exit(&app, exit_code);
        }
    };
    if count == 0 {
        return proceed_exit(&app, exit_code);
    }
    log(
        &app,
        "INFO",
        "application_exit_confirmation_shown",
        serde_json::json!({"active_terminal_sessions": count}),
        None,
    );
    let handle = app.clone();
    app.dialog()
        .message(format!(
            "还有 {count} 个终端会话在运行，退出将结束它们。确定退出吗？"
        ))
        .title("退出 Cosir")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "退出".to_string(),
            "取消".to_string(),
        ))
        .show(move |confirmed| {
            if confirmed {
                log(
                    &handle,
                    "INFO",
                    "application_exit_confirmed",
                    serde_json::json!({}),
                    None,
                );
                proceed_exit(&handle, exit_code);
            } else {
                log(
                    &handle,
                    "INFO",
                    "application_exit_cancelled",
                    serde_json::json!({}),
                    None,
                );
                handle
                    .state::<ExitConfirmation>()
                    .state
                    .store(IDLE, Ordering::Release);
            }
        });
}

fn proceed_exit(app: &AppHandle, exit_code: i32) {
    app.state::<ExitConfirmation>()
        .state
        .store(CONFIRMED, Ordering::Release);
    app.exit(exit_code);
}

fn query_active_count(address: std::net::SocketAddr) -> Result<u64, String> {
    let body = backend_http::get(address, ACTIVE_COUNT_PATH, ACTIVE_COUNT_TIMEOUT)?;
    parse_active_count(&body)
}

fn parse_active_count(body: &str) -> Result<u64, String> {
    let value: serde_json::Value =
        serde_json::from_str(body).map_err(|error| format!("响应不是 JSON：{error}"))?;
    value
        .get("count")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| "响应缺少非负整数 count".to_string())
}

fn log(app: &AppHandle, level: &str, event: &str, data: serde_json::Value, error: Option<String>) {
    app.state::<BackendSupervisor>()
        .log_detail(level, LOG_CALLER, event, data, error);
}

#[cfg(test)]
mod tests {
    use super::parse_active_count;

    #[test]
    fn parses_count_field() {
        assert_eq!(parse_active_count("{\"count\": 3}").unwrap(), 3);
        assert_eq!(parse_active_count("{\"count\":0}").unwrap(), 0);
    }

    #[test]
    fn rejects_malformed_body() {
        assert!(parse_active_count("{}").is_err());
        assert!(parse_active_count("{\"count\": -1}").is_err());
        assert!(parse_active_count("not json").is_err());
    }
}
