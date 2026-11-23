//! 原生 OS 验证：必须在非嵌套受限执行环境运行，不能用 argv 快照代替隔离行为。
#![cfg(any(target_os = "macos", target_os = "linux"))]
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

struct Fixture {
    _temp: tempfile::TempDir,
    workspace: PathBuf,
    outside: PathBuf,
    data: PathBuf,
    scratch: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        for name in ["workspace", "outside", "data/scratch/one"] {
            std::fs::create_dir_all(root.join(name)).unwrap();
        }
        std::fs::write(root.join("data/secret"), "private-secret").unwrap();
        Self {
            workspace: root.join("workspace"),
            outside: root.join("outside"),
            data: root.join("data"),
            scratch: root.join("data/scratch/one"),
            _temp: temp,
        }
    }
    fn policy(&self, full: bool, network: bool, extra: bool) -> Value {
        let mut roots = vec![self.workspace.clone()];
        if extra {
            roots.push(self.outside.clone());
        }
        json!({"full_access":full, "network":network, "writable_roots":roots,"writable_exceptions":[self.scratch],
            "denied_read":[self.data], "readable_exceptions":[self.data.join("scratch")], "readonly_roots":[self.data]})
    }
    fn run(&self, script: &str, policy: Value) -> (String, Value) {
        let mut child = Command::new(env!("CARGO_BIN_EXE_terminal-worker"))
            .args(["--stdio", "--instance-id", "native-sandbox-test"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let frame = serde_json::to_vec(&json!({"type":"start_exec", "program":"/bin/sh", "args":["-c",script], "raw_args":false, "cwd":self.workspace, "policy":policy})).unwrap();
        let mut input = child.stdin.take().unwrap();
        input
            .write_all(&(frame.len() as u32).to_be_bytes())
            .unwrap();
        input.write_all(&frame).unwrap();
        let mut reader = child.stdout.take().unwrap();
        let mut output = String::new();
        let exit = loop {
            let mut size = [0; 4];
            reader
                .read_exact(&mut size)
                .expect("worker closed before exit");
            let mut payload = vec![0; u32::from_be_bytes(size) as usize];
            reader.read_exact(&mut payload).unwrap();
            let event: Value = serde_json::from_slice(&payload).unwrap();
            if event["type"] == "output" {
                use base64::Engine;
                output.push_str(&String::from_utf8_lossy(
                    &base64::engine::general_purpose::STANDARD
                        .decode(event["data_base64"].as_str().unwrap())
                        .unwrap(),
                ));
            }
            if event["type"] == "exit" {
                break event;
            }
        };
        drop(input);
        assert!(child.wait().unwrap().success());
        (output, exit)
    }
}
fn shell(path: &Path) -> String {
    format!("'{}'", path.to_str().unwrap().replace('\'', "'\\''"))
}

#[test]
fn enforces_write_roots_and_per_command_extra_grants() {
    let f = Fixture::new();
    let script = format!(
        "printf local > local; printf external > {}",
        shell(&f.outside.join("file"))
    );
    let (_, exit) = f.run(&script, f.policy(false, false, false));
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(f.workspace.join("local").exists());
    assert!(!f.outside.join("file").exists());
    let (_, exit) = f.run(&script, f.policy(false, false, true));
    assert_eq!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(&script, f.policy(true, true, false));
    assert_eq!(exit["exit_code"], 0, "{exit}");
}

#[test]
fn protects_control_data_while_restoring_only_scratch() {
    let f = Fixture::new();
    let (_, exit) = f.run(
        &format!("cat {}", shell(&f.data.join("secret"))),
        f.policy(false, false, true),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(
        &format!(
            "printf scratch > {}; cat {}",
            shell(&f.scratch.join("file")),
            shell(&f.scratch.join("file"))
        ),
        f.policy(false, false, true),
    );
    assert_eq!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(
        &format!("mv {} {}", shell(&f.data), shell(&f.outside.join("moved"))),
        f.policy(false, false, true),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(f.data.join("secret").exists());
}

#[test]
fn symlink_does_not_expand_writable_scope() {
    let f = Fixture::new();
    std::os::unix::fs::symlink(&f.outside, f.workspace.join("link")).unwrap();
    let (_, exit) = f.run("printf escape > link/file", f.policy(false, false, false));
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(!f.outside.join("file").exists());
}

#[test]
fn network_grant_preserves_filesystem_restrictions() {
    let f = Fixture::new();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let script=format!("/usr/bin/python3 -c 'import socket; socket.create_connection((\"127.0.0.1\",{port}),2); print(\"connected\")'");
    let (_, exit) = f.run(&script, f.policy(false, false, false));
    assert_ne!(exit["exit_code"], 0, "{exit}");
    let (out, exit) = f.run(&script, f.policy(false, true, false));
    assert_eq!(exit["exit_code"], 0, "{exit}: {out}");
    assert!(out.contains("connected"));
    let (_, exit) = f.run(
        &format!("printf forbidden > {}", shell(&f.outside.join("file"))),
        f.policy(false, true, false),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(!f.outside.join("file").exists());
}

#[test]
fn ordinary_grants_inside_control_roots_do_not_become_write_exceptions() {
    let f = Fixture::new();
    let mut policy = f.policy(false, false, false);
    policy["writable_roots"] = json!([f.workspace, f.data]);
    let (_, exit) = f.run(
        &format!("printf changed > {}", shell(&f.data.join("secret"))),
        policy,
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert_eq!(
        std::fs::read_to_string(f.data.join("secret")).unwrap(),
        "private-secret"
    );
}

#[test]
fn broad_parent_grant_cannot_rename_the_ancestor_of_control_code() {
    let f = Fixture::new();
    let control = f.outside.join("control/code");
    std::fs::create_dir_all(&control).unwrap();
    std::fs::write(control.join("program"), "trusted-code").unwrap();
    let mut policy = f.policy(false, false, true);
    policy["writable_roots"] = json!([f.workspace.parent().unwrap()]);
    policy["readonly_roots"] = json!([f.data, control]);
    let moved = f.workspace.parent().unwrap().join("moved");
    let (_, exit) = f.run(
        &format!("mv {} {}", shell(&f.outside), shell(&moved)),
        policy,
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert_eq!(
        std::fs::read_to_string(control.join("program")).unwrap(),
        "trusted-code"
    );
    assert!(!moved.exists());
}

#[cfg(target_os = "linux")]
#[test]
fn socketpair_raw_cannot_be_converted_to_a_host_connected_datagram_socket() {
    let f = Fixture::new();
    for kind in ["SOCK_DGRAM", "SOCK_RAW"] {
        let script =
            format!("python3 -c 'import socket; socket.socketpair(socket.AF_UNIX, socket.{kind})'");
        let (_, exit) = f.run(&script, f.policy(false, true, false));
        assert_ne!(exit["exit_code"], 0, "{kind}: {exit}");
    }
    let (_, exit) = f.run(
        "python3 -c 'import socket; socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)'",
        f.policy(false, true, false),
    );
    assert_eq!(exit["exit_code"], 0, "{exit}");
}

/// 禁读目录即使后端没有重复放进只读集合也不可写；升级授写 `/` 时控制目录与 Worker 自身仍只读。
#[test]
fn escalated_root_grant_keeps_control_roots_and_the_worker_read_only() {
    let f = Fixture::new();
    let control = f.outside.join("control");
    std::fs::create_dir_all(&control).unwrap();
    let control = control.canonicalize().unwrap();
    let mut policy = f.policy(false, true, false);
    policy["writable_roots"] = json!([f.workspace, "/"]);
    policy["readonly_roots"] = json!([control]);
    let (_, exit) = f.run(
        &format!("printf forged > {}", shell(&control.join("forged"))),
        policy.clone(),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(
        &format!("printf forged > {}", shell(&f.data.join("forged"))),
        policy.clone(),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    let worker = Path::new(env!("CARGO_BIN_EXE_terminal-worker")).canonicalize().unwrap();
    // touch 只改时间戳：即使规则失效也不会损坏被测二进制。
    let (_, exit) = f.run(&format!("touch {}", shell(&worker)), policy);
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(!control.join("forged").exists() && !f.data.join("forged").exists());
}

/// 沙箱升级（后端按 ``/`` 授写、开网络）：工作区外可写，系统数据仍读不到、写不进，scratch 照常可写。
#[test]
fn escalated_root_grant_keeps_control_data_denied() {
    let f = Fixture::new();
    let mut policy = f.policy(false, true, false);
    policy["writable_roots"] = json!([f.workspace, "/"]);
    let (_, exit) = f.run(
        &format!(
            "printf ok > {} && printf ok > {}",
            shell(&f.outside.join("escalated")),
            shell(&f.scratch.join("escalated"))
        ),
        policy.clone(),
    );
    assert_eq!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(
        &format!("cat {}", shell(&f.data.join("secret"))),
        policy.clone(),
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    let (_, exit) = f.run(
        &format!("printf forged > {}", shell(&f.data.join("forged"))),
        policy,
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert!(!f.data.join("forged").exists());
}

#[cfg(target_os = "linux")]
#[test]
fn root_write_grant_does_not_restore_the_host_proc_mount() {
    let f = Fixture::new();
    let mut policy = f.policy(false, true, false);
    policy["writable_roots"] = json!(["/"]);
    let (_, exit) = f.run(
        &format!(
            "test ! -e /proc/{}/environ && printf ok > /dev/null",
            std::process::id()
        ),
        policy,
    );
    assert_eq!(exit["exit_code"], 0, "{exit}");
}

#[cfg(target_os = "linux")]
#[test]
fn raw_clone_cannot_create_a_nested_user_namespace() {
    let f = Fixture::new();
    let script = r#"python3 -c '
import ctypes, errno, os, platform
libc = ctypes.CDLL(None, use_errno=True)
nr = 56 if platform.machine() == "x86_64" else 220
pid = libc.syscall(nr, 0x10000000 | 17, 0, 0, 0, 0)
if pid == 0:
    os._exit(0)
if pid > 0:
    os.waitpid(pid, 0)
    raise SystemExit("nested user namespace unexpectedly allowed")
assert ctypes.get_errno() in (errno.EPERM, errno.ENOSPC, errno.EUSERS), ctypes.get_errno()
'"#;
    let (_, exit) = f.run(script, f.policy(false, true, false));
    assert_eq!(exit["exit_code"], 0, "{exit}");
}

/// macOS 的路径匹配按字符串：同一个受保护文件换大小写、经 firmlink 或硬链接访问都不能读到。
#[cfg(target_os = "macos")]
#[test]
fn macos_aliases_and_hard_links_cannot_reach_denied_data() {
    let f = Fixture::new();
    let secret = f.data.join("secret");
    let upper = f
        .data
        .parent()
        .unwrap()
        .join(f.data.file_name().unwrap().to_string_lossy().to_uppercase())
        .join("secret");
    let firmlink = Path::new("/System/Volumes/Data").join(secret.strip_prefix("/").unwrap());
    for alias in [&upper, &firmlink] {
        // 阳性对照：不加沙箱时这些别名确实读得到，用例才有意义。
        let (output, _) = f.run(&format!("cat {}", shell(alias)), f.policy(true, false, false));
        assert!(output.contains("private-secret"), "alias {} is not an alias here", alias.display());
        let (output, exit) = f.run(&format!("cat {}", shell(alias)), f.policy(false, false, false));
        assert!(
            !output.contains("private-secret"),
            "read through {} leaked: {exit}",
            alias.display()
        );
    }
    let control = f.workspace.join("control-link-check");
    let (output, _) = f.run(
        &format!("ln {} {} && cat {}", shell(&secret), shell(&control), shell(&control)),
        f.policy(true, false, false),
    );
    assert!(output.contains("private-secret"), "hard links are not possible here");
    std::fs::remove_file(&control).unwrap();
    let link = f.workspace.join("linked-secret");
    let (output, exit) = f.run(
        &format!("ln {} {} && cat {}", shell(&secret), shell(&link), shell(&link)),
        f.policy(false, false, false),
    );
    assert!(!output.contains("private-secret"), "hard link leaked: {exit}");
}

/// 硬链接也不能借工作区里的另一个名字改写只读控制目录里的文件。
#[cfg(target_os = "macos")]
#[test]
fn macos_hard_link_cannot_write_into_read_only_control_files() {
    let f = Fixture::new();
    let control = f.outside.join("control");
    std::fs::create_dir_all(&control).unwrap();
    let control = control.canonicalize().unwrap();
    let target = control.join("program");
    std::fs::write(&target, "original").unwrap();
    let mut policy = f.policy(false, false, false);
    policy["readonly_roots"] = json!([control, f.data]);
    let link = f.workspace.join("program-link");
    let (_, exit) = f.run(
        &format!("ln {} {} && printf forged >> {}", shell(&target), shell(&link), shell(&link)),
        policy,
    );
    assert_ne!(exit["exit_code"], 0, "{exit}");
    assert_eq!(std::fs::read_to_string(&target).unwrap(), "original");
}
