//! 本地后端健康检查器。

use crate::backend::types::{BackendHealthSnapshot, BackendLaunchConfig};
use reqwest::blocking::Client;
use serde::Deserialize;
use std::time::{Duration, Instant};

/// 后端 `/health` 端点响应体。
#[derive(Debug, Deserialize)]
struct HealthResponsePayload {
    /// 健康状态文本。
    status: String,
    /// 当前模型服务商。
    model_provider: String,
    /// 当前模型基础地址。
    model_base_url: String,
    /// 当前模型名称。
    model_name: String,
    /// 当前 thinking 模式。
    model_thinking_mode: String,
    /// 当前是否已加载 API Key。
    has_model_api_key: bool,
}

/// 读取一次后端健康摘要。
///
/// 参数:
///     config: 后端监听配置。
///
/// 返回:
///     后端可达且响应可解析时返回健康摘要；不可达时返回 `Ok(None)`。
///
/// 异常:
///     当响应体格式错误时返回错误字符串。
///
/// 副作用:
///     发起一次本地 HTTP 请求。
pub fn fetch_backend_health(
    config: &BackendLaunchConfig,
) -> Result<Option<BackendHealthSnapshot>, String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("无法创建本地健康检查客户端: {e}"))?;

    let url = format!("http://{}:{}/health", config.host, config.port);
    let response = match client.get(url).send() {
        Ok(response) => response,
        Err(_) => return Ok(None),
    };

    if !response.status().is_success() {
        return Ok(None);
    }

    let payload: HealthResponsePayload = response
        .json()
        .map_err(|e| format!("无法解析后端 /health 响应: {e}"))?;

    Ok(Some(BackendHealthSnapshot {
        status: payload.status,
        model_provider: payload.model_provider,
        model_base_url: payload.model_base_url,
        model_name: payload.model_name,
        model_thinking_mode: payload.model_thinking_mode,
        has_model_api_key: payload.has_model_api_key,
    }))
}

/// 轮询等待后端健康可用。
///
/// 参数:
///     config: 后端监听配置。
///     timeout: 最大等待时长。
///
/// 返回:
///     超时前探测到健康响应时返回摘要。
///
/// 异常:
///     当超时或健康响应格式错误时返回错误字符串。
///
/// 副作用:
///     在等待窗口内重复访问本地 `/health` 端点。
pub fn wait_for_backend_health(
    config: &BackendLaunchConfig,
    timeout: Duration,
) -> Result<BackendHealthSnapshot, String> {
    let started_at = Instant::now();

    while started_at.elapsed() < timeout {
        if let Some(snapshot) = fetch_backend_health(config)? {
            return Ok(snapshot);
        }
        std::thread::sleep(Duration::from_millis(250));
    }

    Err(format!(
        "等待本地后端健康检查超时（{} 秒）",
        timeout.as_secs()
    ))
}
