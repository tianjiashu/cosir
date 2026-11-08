//! 执行模式的端到端协议：经 stdio 帧驱动真实 worker 二进制（握手、输出、退出、停止、启动失败）。

#![cfg(unix)]

use std::io::{Read, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use serde_json::{json, Value};

struct Worker {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: ChildStdout,
}

impl Worker {
    fn start(frame: Value) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_terminal-worker"))
            .args(["--stdio", "--instance-id", "exec-test"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn worker");
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("stdout");
        let mut worker = Self {
            child,
            stdin,
            stdout,
        };
        worker.send(frame);
        worker
    }

    fn send(&mut self, frame: Value) {
        let payload = serde_json::to_vec(&frame).unwrap();
        let stdin = self.stdin.as_mut().expect("stdin open");
        stdin
            .write_all(&(payload.len() as u32).to_be_bytes())
            .unwrap();
        stdin.write_all(&payload).unwrap();
        stdin.flush().unwrap();
    }

    /// 读到 worker 关闭 stdout 为止。
    fn events(&mut self) -> Vec<Value> {
        let mut events = Vec::new();
        let mut header = [0_u8; 4];
        while self.stdout.read_exact(&mut header).is_ok() {
            let mut payload = vec![0_u8; u32::from_be_bytes(header) as usize];
            self.stdout.read_exact(&mut payload).unwrap();
            events.push(serde_json::from_slice(&payload).unwrap());
        }
        events
    }

    fn next_event(&mut self) -> Value {
        let mut header = [0_u8; 4];
        self.stdout.read_exact(&mut header).unwrap();
        let mut payload = vec![0_u8; u32::from_be_bytes(header) as usize];
        self.stdout.read_exact(&mut payload).unwrap();
        serde_json::from_slice(&payload).unwrap()
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        drop(self.stdin.take());
        let _ = self.child.wait();
    }
}

fn exec(script: &str) -> Value {
    json!({"type": "start_exec", "program": "/bin/sh", "args": ["-c", script], "raw_args": false, "cwd": "/"})
}

fn output(events: &[Value]) -> String {
    events
        .iter()
        .filter(|event| event["type"] == "output")
        .map(|event| {
            String::from_utf8(
                BASE64
                    .decode(event["data_base64"].as_str().unwrap())
                    .unwrap(),
            )
            .unwrap()
        })
        .collect()
}

#[test]
fn runs_a_command_and_reports_handshake_output_and_exit() {
    let mut worker = Worker::start(exec("echo hello; exit 4"));
    let events = worker.events();
    assert_eq!(events[0]["type"], "handshake");
    assert_eq!(events[0]["mode"], "exec");
    assert!(events[0].get("pty_kind").is_none());
    assert_eq!(output(&events), "hello\n");
    assert_eq!(
        events.last().unwrap(),
        &json!({"type": "exit", "reason": "exited", "exit_code": 4})
    );
}

#[test]
fn shutdown_stops_the_running_command() {
    let started = Instant::now();
    let mut worker = Worker::start(exec("echo ready; exec sleep 30"));
    assert_eq!(worker.next_event()["type"], "handshake");
    assert_eq!(output(&[worker.next_event()]), "ready\n");
    worker.send(json!({"type": "shutdown"}));
    let events = worker.events();
    // sleep 收到 HUP 即退出，不用等满宽限期。
    assert_eq!(
        events.last().unwrap(),
        &json!({"type": "exit", "reason": "stopped", "signal": libc::SIGHUP})
    );
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn closing_stdin_stops_the_command_like_a_killed_backend() {
    let mut worker = Worker::start(exec("echo ready; exec sleep 30"));
    assert_eq!(worker.next_event()["type"], "handshake");
    assert_eq!(output(&[worker.next_event()]), "ready\n");
    drop(worker.stdin.take());
    let events = worker.events();
    assert_eq!(events.last().unwrap()["type"], "exit");
    assert_eq!(events.last().unwrap()["reason"], "stopped");
}

/// 命令先自己结束、停止请求后到：结局仍是命令自己的退出，停止不改写它。留下一个忽略 HUP / TERM 的
/// 进程，让 worker 在清理宽限期里还活着，停止请求才能在命令退出之后、`exit` 之前到达。
#[test]
fn a_stop_that_arrives_after_the_command_exited_keeps_the_exit() {
    let mut worker = Worker::start(exec("(trap '' HUP TERM; exec sleep 5) & echo done"));
    assert_eq!(worker.next_event()["type"], "handshake");
    assert_eq!(output(&[worker.next_event()]), "done\n");
    std::thread::sleep(Duration::from_millis(100));
    worker.send(json!({"type": "shutdown"}));
    let events = worker.events();
    assert_eq!(
        events.last().unwrap(),
        &json!({"type": "exit", "reason": "exited", "exit_code": 0})
    );
}

#[test]
fn input_frames_are_rejected_without_stopping_the_command() {
    let mut worker = Worker::start(exec("sleep 0.3; echo done"));
    assert_eq!(worker.next_event()["type"], "handshake");
    worker.send(json!({"type": "write", "input": {"type": "line", "text": "x"}}));
    worker.send(json!({"type": "signal", "request_id": "r1", "signal": "interrupt"}));
    let events = worker.events();
    assert!(events
        .iter()
        .any(|event| event["type"] == "error" && event["code"] == "input_rejected"));
    assert!(events.iter().any(|event| event["type"] == "signal_result"
        && event["request_id"] == "r1"
        && event["status"] == "unsupported"));
    assert_eq!(output(&events), "done\n");
    assert_eq!(
        events.last().unwrap(),
        &json!({"type": "exit", "reason": "exited", "exit_code": 0})
    );
}

#[test]
fn a_command_that_cannot_start_is_reported_after_the_handshake() {
    let mut worker = Worker::start(json!({
        "type": "start_exec", "program": "/no/such/program", "args": [], "raw_args": false, "cwd": "/"
    }));
    let events = worker.events();
    assert_eq!(events[0]["mode"], "exec");
    assert_eq!(events[1]["type"], "exit");
    assert_eq!(events[1]["reason"], "start_failed");
    assert!(events[1]["message"].as_str().is_some_and(|m| !m.is_empty()));
    assert_eq!(events.len(), 2);
}
