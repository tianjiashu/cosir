use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

/// 后端的启动方式。运行时解析与进程创建分离，避免 supervisor 了解环境变量细节。
#[derive(Debug, Clone)]
pub enum BackendRuntime {
    UvProject {
        launcher: PathBuf,
        backend_dir: PathBuf,
        cache_dir: PathBuf,
    },
    Interpreter {
        launcher: PathBuf,
        backend_dir: PathBuf,
    },
}

impl BackendRuntime {
    pub fn backend_dir(&self) -> &Path {
        match self {
            Self::UvProject { backend_dir, .. } | Self::Interpreter { backend_dir, .. } => {
                backend_dir
            }
        }
    }

    pub fn launcher(&self) -> &Path {
        match self {
            Self::UvProject { launcher, .. } | Self::Interpreter { launcher, .. } => launcher,
        }
    }

    pub fn uv_cache_dir(&self) -> Option<&Path> {
        match self {
            Self::UvProject { cache_dir, .. } => Some(cache_dir),
            Self::Interpreter { .. } => None,
        }
    }
}

/// 解析本地桌面应用应使用的后端运行时。
pub fn resolve_backend_runtime(
    app: &AppHandle,
    runtime_dir: &Path,
) -> Result<BackendRuntime, String> {
    if cfg!(debug_assertions) {
        let backend_dir = std::env::var_os("COSIR_BACKEND_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(default_backend_dir);
        if let Some(launcher) = std::env::var_os("COSIR_BACKEND_PYTHON").map(PathBuf::from) {
            return Ok(BackendRuntime::Interpreter {
                launcher,
                backend_dir,
            });
        }
        if std::process::Command::new("uv")
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_err()
        {
            return Err(
                "开发环境未找到 uv，请安装 uv 或显式设置 COSIR_BACKEND_PYTHON 指向 Python 解释器"
                    .to_string(),
            );
        }
        let cache_dir = runtime_dir.join("uv-cache");
        std::fs::create_dir_all(&cache_dir).map_err(|error| {
            format!(
                "无法创建项目级 uv 缓存目录：{}：{error}",
                cache_dir.display()
            )
        })?;
        return Ok(BackendRuntime::UvProject {
            launcher: PathBuf::from("uv"),
            backend_dir,
            cache_dir,
        });
    }

    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法解析随应用交付的后端运行时目录：{error}"))?;
    let backend_dir = resource_dir.join("backend");
    if !backend_dir.is_dir() {
        return Err(format!(
            "随应用交付的后端代码目录不存在：{}",
            backend_dir.display()
        ));
    }
    let runtime_root = resource_dir.join("backend-runtime");
    let launcher = packaged_python(&runtime_root);
    if !launcher.is_file() {
        return Err(format!(
            "随应用交付的后端 Python 运行时不存在：{}",
            launcher.display()
        ));
    }
    Ok(BackendRuntime::Interpreter {
        launcher,
        backend_dir,
    })
}

fn default_backend_dir() -> PathBuf {
    if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("backend")
    } else {
        PathBuf::from("backend")
    }
}

fn packaged_python(runtime_root: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        runtime_root.join("python.exe")
    }
    #[cfg(not(windows))]
    {
        runtime_root.join("bin").join("python")
    }
}

#[cfg(test)]
mod tests {
    use super::{packaged_python, BackendRuntime};
    use std::path::PathBuf;

    #[test]
    fn runtime_properties_keep_uv_details_inside_uv_variant() {
        let runtime = BackendRuntime::UvProject {
            launcher: PathBuf::from("uv"),
            backend_dir: PathBuf::from("backend"),
            cache_dir: PathBuf::from("runtime/uv-cache"),
        };
        assert_eq!(
            runtime.uv_cache_dir(),
            Some(std::path::Path::new("runtime/uv-cache"))
        );
    }

    #[test]
    fn packaged_python_has_platform_specific_location() {
        let path = packaged_python(std::path::Path::new("resources/backend-runtime"));
        assert!(path.ends_with(if cfg!(windows) {
            "python.exe"
        } else {
            "bin/python"
        }));
    }
}
