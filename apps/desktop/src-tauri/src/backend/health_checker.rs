//! 本地后端健康检查器。

use crate::backend::types::{BackendHealthSnapshot, BackendLaunchConfig};
use reqwest::Client;
use serde::Deserialize;
use std::time::Duration;

/// 后端 `/health` 端点响应体。
#[derive(Debug, Deserialize)]
struct HealthResponsePayload {
    /// 健康状态文本。
    status: String,
    /// 当前模型服务商。
    model_provider: String,
    /// 当前模型名称。
    model_name: String,
    /// 当前 thinking 模式。
    model_thinking_mode: String,
    /// 当前是否已加载 API Key。
    has_model_api_key: bool,
}

/// 异步读取一次后端健康摘要。
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
///     发起一次本地 HTTP 请求（异步等待，不阻塞调用线程）。
pub async fn fetch_backend_health(
    config: &BackendLaunchConfig,
) -> Result<Option<BackendHealthSnapshot>, String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("无法创建本地健康检查客户端: {e}"))?;

    let url = format!("http://{}:{}/health", config.host, config.port);
    let response = match client.get(url).send().await {
        Ok(response) => response,
        Err(_) => return Ok(None),
    };

    if !response.status().is_success() {
        return Ok(None);
    }

    let payload: HealthResponsePayload = response
        .json()
        .await
        .map_err(|e| format!("无法解析后端 /health 响应: {e}"))?;

    Ok(Some(BackendHealthSnapshot {
        status: payload.status,
        model_provider: payload.model_provider,
        model_name: payload.model_name,
        model_thinking_mode: payload.model_thinking_mode,
        has_model_api_key: payload.has_model_api_key,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::types::BackendLaunchConfig;
    use std::path::PathBuf;

    fn unreachable_config() -> BackendLaunchConfig {
        BackendLaunchConfig {
            repo_root: PathBuf::from("/tmp"),
            backend_dir: PathBuf::from("/tmp/backend"),
            python_binary: PathBuf::from("/tmp/backend/.venv/bin/python"),
            host: "127.0.0.1".to_string(),
            // 使用一个几乎不可能有服务监听的端口，触发连接失败分支。
            port: 1,
            app_log_file: PathBuf::from("/tmp/app.log"),
            stdout_log_file: PathBuf::from("/tmp/stdout.log"),
            stderr_log_file: PathBuf::from("/tmp/stderr.log"),
            boot_state_file: PathBuf::from("/tmp/boot.json"),
        }
    }

    #[tokio::test]
    async fn test_fetch_health_unreachable_returns_none() {
        // 后端不可达时不应返回 Err，而应返回 Ok(None)（静默降级到状态轮询）。
        let result = fetch_backend_health(&unreachable_config()).await;
        assert!(result.is_ok());
        assert!(result.unwrap().is_none());
    }
}
