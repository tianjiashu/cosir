//! 仅包装 Agent 命令；交互会话不经过这里。策略事实由后端提供，平台层只负责落实。

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
mod linux_seccomp;
#[cfg(target_os = "macos")]
mod macos;
mod policy;

pub use policy::ExecutionPolicy;
use std::process::Command;

/// 包装前保持 argv 边界，不把命令再拼成一层 shell 文本。
pub fn command(
    program: &str,
    args: &[String],
    policy: &ExecutionPolicy,
) -> anyhow::Result<Command> {
    if policy.full_access {
        let mut command = Command::new(program);
        command.args(args);
        return Ok(command);
    }
    policy.validate()?;
    #[cfg(target_os = "macos")]
    {
        macos::command(program, args, policy)
    }
    #[cfg(target_os = "linux")]
    {
        linux::command(program, args, policy)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        anyhow::bail!("terminal sandbox is not supported on this platform")
    }
}

/// 真正运行一次最小沙箱；探测失败不允许降级裸跑。
pub fn probe() -> anyhow::Result<()> {
    let root = tempfile::tempdir()?;
    let policy = ExecutionPolicy {
        full_access: false,
        network: false,
        writable_roots: vec![root.path().canonicalize()?],
        writable_exceptions: Vec::new(),
        denied_read: Vec::new(),
        readable_exceptions: Vec::new(),
        readonly_roots: Vec::new(),
    };
    let result = command("/bin/sh", &["-c".into(), "exit 0".into()], &policy)?.status()?;
    anyhow::ensure!(
        result.success(),
        "the operating system refused the terminal sandbox"
    );
    Ok(())
}
