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
