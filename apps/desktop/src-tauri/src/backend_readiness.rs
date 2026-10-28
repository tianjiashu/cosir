use std::net::{SocketAddr, ToSocketAddrs};
use std::path::Path;
use std::time::{Duration, Instant};

use crate::backend_http;

const PROBE_TIMEOUT: Duration = Duration::from_millis(500);

pub fn wait_for_backend(
    host: &str,
    port: u16,
    timeout: Duration,
    bootstate_file: &Path,
    process_alive: impl Fn() -> bool,
) -> Result<(), String> {
    let address = (host, port)
        .to_socket_addrs()
        .map_err(|error| format!("无法解析后端地址：{error}"))?
        .next()
        .ok_or_else(|| "后端地址没有可用解析结果".to_string())?;
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !process_alive() {
            return Err("本地 Agent 后端进程在健康检查前退出".to_string());
        }
        if let Some(error) = read_bootstate_failure(bootstate_file) {
            return Err(error);
        }
        if probe_health(address).is_ok() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!("后端在 {} 秒内未通过健康检查", timeout.as_secs()))
}

fn read_bootstate_failure(path: &Path) -> Option<String> {
    let content = std::fs::read_to_string(path).ok()?;
    let state: serde_json::Value = serde_json::from_str(&content).ok()?;
    if state.get("phase")?.as_str()? != "failed" {
        return None;
    }
    let message = state
        .get("error_message")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("后端启动失败");
    let error_type = state
        .get("error_type")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("启动错误");
    Some(format!(
        "本地 Agent 后端启动失败（{error_type}）：{message}"
    ))
}

fn probe_health(address: SocketAddr) -> Result<(), String> {
    backend_http::get(address, "/health", PROBE_TIMEOUT).map(|_| ())
}
