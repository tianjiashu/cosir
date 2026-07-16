//! 本地后端启动状态读取器。

use crate::backend::types::BackendLaunchConfig;
use serde::Deserialize;
use std::fs;

/// 后端启动状态文件中的阶段取值。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootPhase {
    /// 启动中。
    Booting,
    /// 应用装配成功，等待端口就绪。
    Ready,
    /// 启动失败。
    Failed,
    /// 优雅关闭。
    Stopped,
    /// 无法识别的阶段文本。
    Unknown,
}

/// 后端启动状态文件内容。
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct BackendBootState {
    /// 当前启动阶段文本。
    pub phase: String,
    /// 可选的子步骤名。
    pub step: Option<String>,
    /// 失败时的异常类型名。
    pub error_type: Option<String>,
    /// 失败时的异常消息。
    pub error_message: Option<String>,
    /// 失败时的完整 traceback。
    pub traceback: Option<String>,
}

impl BackendBootState {
    /// 将阶段文本解析为枚举。
    ///
    /// 参数:
    ///     无。
    ///
    /// 返回:
    ///     对应的 `BootPhase`；文本不匹配已知阶段时返回 `BootPhase::Unknown`。
    pub fn phase_kind(&self) -> BootPhase {
        match self.phase.as_str() {
            "booting" => BootPhase::Booting,
            "ready" => BootPhase::Ready,
            "failed" => BootPhase::Failed,
            "stopped" => BootPhase::Stopped,
            _ => BootPhase::Unknown,
        }
    }
}

/// 读取后端启动状态文件。
///
/// 参数:
///     config: 后端启动配置，提供启动状态文件路径。
///
/// 返回:
///     文件存在且可解析为 `BackendBootState` 时返回其内容；否则返回 `None`。
///
/// 异常:
///     无；文件缺失或解析失败均视为 `None`。
///
/// 副作用:
///     读取本地文件。
pub fn read_boot_state(config: &BackendLaunchConfig) -> Option<BackendBootState> {
    if !config.boot_state_file.exists() {
        return None;
    }
    let content = fs::read_to_string(&config.boot_state_file).ok()?;
    serde_json::from_str(&content).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::types::BackendLaunchConfig;
    use std::path::PathBuf;

    fn test_config(file: &str) -> BackendLaunchConfig {
        BackendLaunchConfig {
            repo_root: PathBuf::from("/tmp"),
            backend_dir: PathBuf::from("/tmp/backend"),
            python_binary: PathBuf::from("/tmp/backend/.venv/bin/python"),
            host: "127.0.0.1".to_string(),
            port: 8000,
            app_log_file: PathBuf::from("/tmp/app.log"),
            stdout_log_file: PathBuf::from("/tmp/stdout.log"),
            stderr_log_file: PathBuf::from("/tmp/stderr.log"),
            boot_state_file: PathBuf::from(file),
        }
    }

    #[test]
    fn test_phase_kind_all_known() {
        assert_eq!(
            BackendBootState {
                phase: "booting".into(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Booting
        );
        assert_eq!(
            BackendBootState {
                phase: "ready".into(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Ready
        );
        assert_eq!(
            BackendBootState {
                phase: "failed".into(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Failed
        );
        assert_eq!(
            BackendBootState {
                phase: "stopped".into(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Stopped
        );
    }

    #[test]
    fn test_phase_kind_unknown() {
        assert_eq!(
            BackendBootState {
                phase: "weird".into(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Unknown
        );
        // 空字符串也应落入 Unknown，而非 panic。
        assert_eq!(
            BackendBootState {
                phase: String::new(),
                step: None,
                error_type: None,
                error_message: None,
                traceback: None,
            }
            .phase_kind(),
            BootPhase::Unknown
        );
    }

    #[test]
    fn test_read_boot_state_missing_file() {
        let config = test_config("/tmp/non_existent_bootstate_12345.json");
        assert!(read_boot_state(&config).is_none());
    }

    #[test]
    fn test_read_boot_state_malformed_json() {
        let dir = std::env::temp_dir().join("coding_agent_test_bootstate");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("malformed.json");
        std::fs::write(&path, "{not valid json").unwrap();
        let config = test_config(path.to_str().unwrap());
        assert!(read_boot_state(&config).is_none());
    }

    #[test]
    fn test_read_boot_state_valid() {
        let dir = std::env::temp_dir().join("coding_agent_test_bootstate");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("valid.json");
        std::fs::write(
            &path,
            r#"{"phase":"failed","step":"start","error_type":"RuntimeError","error_message":"boom","traceback":"Traceback..."}"#,
        )
        .unwrap();
        let config = test_config(path.to_str().unwrap());
        let state = read_boot_state(&config).expect("应解析成功");
        assert_eq!(state.phase_kind(), BootPhase::Failed);
        assert_eq!(state.step.as_deref(), Some("start"));
        assert_eq!(state.error_type.as_deref(), Some("RuntimeError"));
        assert_eq!(state.error_message.as_deref(), Some("boom"));
        assert!(state.traceback.is_some());
    }
}
