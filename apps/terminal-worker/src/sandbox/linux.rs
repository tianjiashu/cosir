use super::{linux_seccomp, ExecutionPolicy};
use std::collections::BTreeSet;
use std::io::{Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::Command;

pub fn command(
    program: &str,
    args: &[String],
    policy: &ExecutionPolicy,
) -> anyhow::Result<Command> {
    let executable = std::env::current_exe()?;
    let bwrap = executable
        .parent()
        .ok_or_else(|| anyhow::anyhow!("worker directory unavailable"))?
        .join("cosir-bwrap");
    anyhow::ensure!(bwrap.is_file(), "bundled cosir-bwrap is unavailable");
    let mut command = Command::new(bwrap);
    command.args([
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
    ]);
    if !policy.network {
        command.arg("--unshare-net");
    }
    for root in &policy.writable_roots {
        command.arg("--bind").arg(root).arg(root);
    }
    // 固定可写区内保护路径的祖先为挂载点，防止整目录改名后重建旧程序路径。
    let read_only = policy.read_only_paths()?;
    let ancestors: BTreeSet<_> = read_only
        .iter()
        .flat_map(|path| path.ancestors().skip(1))
        .filter(|path| {
            policy
                .writable_roots
                .iter()
                .any(|root| path.starts_with(root))
        })
        .collect();
    for ancestor in ancestors {
        command.arg("--bind").arg(ancestor).arg(ancestor);
    }
    for root in &read_only {
        command.arg("--ro-bind").arg(root).arg(root);
    }
    // 固定系统视图放在所有宽泛授权之后，批准 / 也不能重新暴露宿主 proc 或设备。
    command.args(["--proc", "/proc", "--dev", "/dev"]);
    // 遮蔽必须晚于只读 bind，否则上层只读 bind 会把秘密重新暴露。
    for root in &policy.denied_read {
        if root.is_dir() {
            command.arg("--tmpfs").arg(root);
        } else {
            command.arg("--ro-bind").arg("/dev/null").arg(root);
        }
    }
    for root in &policy.readable_exceptions {
        command.arg("--ro-bind").arg(root).arg(root);
    }
    // scratch 是保护数据根里的显式写例外，必须在遮蔽之后恢复。
    for root in &policy.writable_exceptions {
        command.arg("--bind").arg(root).arg(root);
    }
    for root in &policy.denied_read {
        if root.is_dir() {
            command.arg("--remount-ro").arg(root);
        }
    }
    let mut filter = tempfile::tempfile()?;
    filter.write_all(&linux_seccomp::compile(policy.network)?)?;
    filter.seek(SeekFrom::Start(0))?;
    let fd = filter.as_raw_fd();
    command
        .arg("--seccomp")
        .arg(fd.to_string())
        .arg("--")
        .arg(program)
        .args(args);
    // fork 后只做 async-signal-safe 的 fd 操作；规则、分配和日志均在父进程完成。
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(filter.as_raw_fd(), libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(command)
}
