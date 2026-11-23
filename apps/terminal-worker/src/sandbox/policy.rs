use serde::Deserialize;
use std::path::PathBuf;

/// 一条已获准命令的完整权限；不含用户偏好，也不持有批准请求。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionPolicy {
    pub full_access: bool,
    // 未实现沙箱的平台仍解析同一 wire，保留该事实但不执行网络规则。
    #[cfg_attr(not(any(target_os = "macos", target_os = "linux")), allow(dead_code))]
    pub network: bool,
    pub writable_roots: Vec<PathBuf>,
    pub writable_exceptions: Vec<PathBuf>,
    pub denied_read: Vec<PathBuf>,
    pub readable_exceptions: Vec<PathBuf>,
    pub readonly_roots: Vec<PathBuf>,
}

impl ExecutionPolicy {
    /// 一律只读的路径：后端给的控制目录、禁读目录（禁读的自然也不可写，不依赖后端把它重复放进
    /// 只读集合），以及 Worker 自己与随附的 `cosir-bwrap`——它们由 Worker 自己认定，后端不必知道位置。
    pub fn read_only_paths(&self) -> anyhow::Result<Vec<PathBuf>> {
        let mut paths: Vec<PathBuf> = self
            .readonly_roots
            .iter()
            .chain(&self.denied_read)
            .cloned()
            .collect();
        let worker = std::env::current_exe()?.canonicalize()?;
        if let Some(bwrap) = worker.parent().map(|dir| dir.join("cosir-bwrap")) {
            if bwrap.is_file() {
                paths.push(bwrap.canonicalize()?);
            }
        }
        paths.push(worker);
        paths.sort();
        paths.dedup();
        Ok(paths)
    }

    pub fn validate(&self) -> anyhow::Result<()> {
        for path in self
            .writable_roots
            .iter()
            .chain(&self.denied_read)
            .chain(&self.readable_exceptions)
            .chain(&self.readonly_roots)
            .chain(&self.writable_exceptions)
        {
            anyhow::ensure!(path.is_absolute(), "sandbox policy requires absolute paths");
            // 后端在批准前解析真实目录；等待期间被替换的路径不能获得旧批准。
            anyhow::ensure!(
                path.canonicalize()? == *path,
                "sandbox path changed before launch: {}",
                path.display()
            );
        }
        Ok(())
    }
}
