//! 文件系统由 bwrap 执行；此处独立编译始终生效的进程/IPC 限制和本次 IP 网络权限。
use seccompiler::{
    BpfProgram, SeccompAction, SeccompCmpArgLen, SeccompCmpOp, SeccompCondition, SeccompFilter,
    SeccompRule, TargetArch,
};
use std::collections::BTreeMap;

pub fn compile(network: bool) -> anyhow::Result<Vec<u8>> {
    let mut rules = BTreeMap::new();
    for nr in [
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
        libc::SYS_request_key,
        libc::SYS_io_uring_setup,
        libc::SYS_io_uring_enter,
        libc::SYS_io_uring_register,
        libc::SYS_mount,
        libc::SYS_umount2,
        libc::SYS_setns,
        libc::SYS_unshare,
        libc::SYS_open_by_handle_at,
    ] {
        rules.insert(nr, Vec::new());
    }
    let condition =
        |index, op, value| SeccompCondition::new(index, SeccompCmpArgLen::Dword, op, value);
    let socket_rules = if network {
        vec![SeccompRule::new(vec![
            condition(0, SeccompCmpOp::Ne, libc::AF_INET as u64)?,
            condition(0, SeccompCmpOp::Ne, libc::AF_INET6 as u64)?,
        ])?]
    } else {
        Vec::new()
    };
    rules.insert(libc::SYS_socket, socket_rules);
    // 匿名 stream pair 供子进程管理使用；不开放可重新连接宿主 socket 的 datagram pair。
    let mut pair_rules = vec![SeccompRule::new(vec![condition(
        0,
        SeccompCmpOp::Ne,
        libc::AF_UNIX as u64,
    )?])?];
    for socket_type in (0..16).filter(|value| *value != libc::SOCK_STREAM) {
        pair_rules.push(SeccompRule::new(vec![condition(
            1,
            SeccompCmpOp::MaskedEq(0xf),
            socket_type as u64,
        )?])?);
    }
    rules.insert(libc::SYS_socketpair, pair_rules);
    #[cfg(target_arch = "x86_64")]
    {
        // x32 共用 audit arch，整段拒绝，避免另一个 syscall 编号空间绕过规则。
        for nr in 0..=550 {
            rules.insert(0x4000_0000 | nr, Vec::new());
        }
    }
    let arch = if cfg!(target_arch = "x86_64") {
        TargetArch::x86_64
    } else if cfg!(target_arch = "aarch64") {
        TargetArch::aarch64
    } else {
        anyhow::bail!("unsupported Linux sandbox architecture")
    };
    let program: BpfProgram = SeccompFilter::new(
        rules,
        SeccompAction::Allow,
        SeccompAction::Errno(libc::EPERM as u32),
        arch,
    )?
    .try_into()?;
    // sock_filter 的 UAPI 布局固定为 code:u16,jt:u8,jf:u8,k:u32；避免转译中引入未初始化填充。
    let mut bytes = Vec::with_capacity(program.len() * 8);
    for instruction in program {
        bytes.extend_from_slice(&instruction.code.to_ne_bytes());
        bytes.extend([instruction.jt, instruction.jf]);
        bytes.extend_from_slice(&instruction.k.to_ne_bytes());
    }
    Ok(bytes)
}
