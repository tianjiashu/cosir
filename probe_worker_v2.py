"""临时探针：直接驱动 terminal-worker 二进制做行为验证（用后即删）。"""

import base64
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time

CWD = os.getcwd()
SEND_LOCK = threading.Lock()


def frame(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def send(proc, message: dict) -> None:
    with SEND_LOCK:
        proc.stdin.write(frame(message))
        proc.stdin.flush()


def read_events(proc, seconds: float):
    """非阻塞收集 worker 事件。"""

    import select

    events = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            continue
        header = proc.stdout.read(4)
        if len(header) < 4:
            break
        (size,) = struct.unpack(">I", header)
        body = proc.stdout.read(size)
        if len(body) < size:
            break
        events.append(json.loads(body.decode("utf-8")))
    return events


def output_text(events):
    chunks = []
    for event in events:
        if event.get("type") == "output":
            chunks.append(base64.b64decode(event["data_base64"]).decode("utf-8", "replace"))
    return "".join(chunks)


def children_of(pid):
    result = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(v) for v in result.stdout.split()]


def alive(pid):
    result = subprocess.run(["ps", "-o", "stat=,command=", "-p", str(pid)], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def start_worker(binary):
    proc = subprocess.Popen(
        [binary, "--stdio", "--instance-id", "probe" + str(os.getpid())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=CWD,
        bufsize=0,
    )
    send(proc, {"type": "start", "shell": ["/bin/zsh"], "shell_kind": "zsh", "cwd": CWD})
    events = read_events(proc, 2.0)
    handshake = next((e for e in events if e.get("type") == "handshake"), None)
    thread = threading.Thread(target=heartbeat_loop, args=(proc,), daemon=True)
    thread.start()
    return proc, handshake, events


def heartbeat_loop(proc, interval: float = 5.0) -> None:
    """按协议维持 heartbeat，避免 worker 触发 HEARTBEAT_TIMEOUT。"""

    while proc.poll() is None:
        time.sleep(interval)
        try:
            send(proc, {"type": "heartbeat"})
        except (BrokenPipeError, ValueError, OSError):
            return


def cleanup(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def kill_descendants(pid):
    """杀掉 worker 会话内残留进程，避免污染后续测试。"""

    for child in children_of(pid):
        kill_descendants(child)
    cleanup([pid])


def scenario_shutdown(binary):
    print("\n===== 场景A：shutdown 时的进程树回收 =====")
    proc, handshake, _ = start_worker(binary)
    if handshake is None:
        print("  !! 无 handshake，stderr:", proc.stderr.read(500))
        proc.kill()
        return
    time.sleep(0.8)
    send(proc, {"type": "write", "data_base64": base64.b64encode(b"sleep 300 & sleep 301 & sleep 302 & sleep 303 &\n").decode()})
    time.sleep(1.5)
    shell_pids = children_of(proc.pid)
    sleeps = []
    for shell in shell_pids:
        sleeps.extend(children_of(shell))
    print(f"  worker={proc.pid} shell={shell_pids} 后台子进程={sleeps}")
    send(proc, {"type": "shutdown"})
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = "TIMEOUT"
        proc.kill()
    time.sleep(0.6)
    left = [pid for pid in sleeps + shell_pids if alive(pid)]
    print(f"  worker 退出码={code}，关闭后仍存活={left or 'none'}")
    print(f"  结论: {'通过（无孤儿）' if not left else 'FAIL：遗留 ' + str(left)}")
    cleanup(sleeps + shell_pids)


def scenario_suspend(binary):
    print("\n===== 场景B：suspend 语义 =====")
    proc, handshake, _ = start_worker(binary)
    if handshake is None:
        proc.kill()
        return
    time.sleep(0.8)
    send(proc, {"type": "write", "data_base64": base64.b64encode(b"sleep 15; echo AFTER_TSTP\n").decode()})
    time.sleep(1.2)
    shell_pids = children_of(proc.pid)
    job_pids = []
    for shell in shell_pids:
        job_pids.extend(children_of(shell))
    def job_states():
        if not job_pids:
            return "no job pid"
        out = subprocess.run(
            ["ps", "-o", "pid=,stat=,command=", "-p", ",".join(str(p) for p in job_pids)],
            capture_output=True, text=True,
        ).stdout.strip()
        return out or "job 进程已不存在"
    print(f"  suspend 前后台作业进程: {job_states()}")
    send(proc, {"type": "signal", "signal": "suspend"})
    events = read_events(proc, 3.0)
    text = output_text(events)
    errors = [e.get("code") for e in events if e.get("type") == "error"]
    print(f"  输出片段: {text!r}")
    print(f"  error 事件: {errors or 'none'}")
    print(f"  suspend 后作业进程: {job_states()}")
    print(f"  AFTER_TSTP 是否执行: {'是（suspend 未冻结整条命令）' if 'AFTER_TSTP' in text else '否'}")
    print(f"  worker 是否存活: {proc.poll() is None}")
    print(f"  suspend 后 shell 进程: {[(p, alive(p)) for p in shell_pids]}")
    if proc.poll() is not None:
        print(f"  !! worker 退出码={proc.poll()}")
        stderr = proc.stderr.read() or b""
        print(f"  !! worker stderr: {stderr.decode(errors='replace')[:600]!r}")
        print(f"  !! worker 剩余事件: {read_events(proc, 0.5)}")
    else:
        send(proc, {"type": "write", "data_base64": base64.b64encode(b"jobs\n").decode()})
        jobs_text = output_text(read_events(proc, 2.0))
        print(f"  suspend 后 jobs 输出: {jobs_text!r}")
    send(proc, {"type": "shutdown"})
    proc.wait(timeout=10)
    kill_descendants(proc.pid)


def scenario_eof_interrupt(binary):
    print("\n===== 场景C：interrupt / eof 语义 =====")
    proc, handshake, _ = start_worker(binary)
    if handshake is None:
        proc.kill()
        return
    time.sleep(0.8)
    send(proc, {"type": "write", "data_base64": base64.b64encode(b"sleep 30; echo INT_UNEXPECTED\n").decode()})
    time.sleep(1.0)
    send(proc, {"type": "signal", "signal": "interrupt"})
    events = read_events(proc, 2.5)
    text = output_text(events)
    print(f"  interrupt 输出: {text!r}")
    print(f"  INT_UNEXPECTED 是否执行: {'是（FAIL）' if 'INT_UNEXPECTED' in text else '否（通过）'}")
    print(f"  error 事件: {[e.get('code') for e in events if e.get('type') == 'error'] or 'none'}")

    send(proc, {"type": "write", "data_base64": base64.b64encode(b"cat\n").decode()})
    time.sleep(0.8)
    send(proc, {"type": "signal", "signal": "eof"})
    events = read_events(proc, 2.0)
    print(f"  eof 后是否回到提示符: {'%' in output_text(events)}")
    print(f"  eof error 事件: {[e.get('code') for e in events if e.get('type') == 'error'] or 'none'}")
    send(proc, {"type": "shutdown"})
    proc.wait(timeout=10)
    kill_descendants(proc.pid)


def scenario_stdin_eof(binary):
    print("\n===== 场景D：worker stdin 关闭（backend 崩溃路径）=====")
    proc, handshake, _ = start_worker(binary)
    if handshake is None:
        proc.kill()
        return
    time.sleep(0.8)
    send(proc, {"type": "write", "data_base64": base64.b64encode(b"sleep 300 & sleep 301 &\n").decode()})
    time.sleep(1.5)
    shell_pids = children_of(proc.pid)
    sleeps = []
    for shell in shell_pids:
        sleeps.extend(children_of(shell))
    print(f"  worker={proc.pid} shell={shell_pids} 后台子进程={sleeps}")
    proc.stdin.close()
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = "TIMEOUT"
        proc.kill()
    time.sleep(0.6)
    left = [pid for pid in sleeps + shell_pids if alive(pid)]
    print(f"  worker 退出码={code}，关闭后仍存活={left or 'none'}")
    print(f"  结论: {'通过（无孤儿）' if not left else 'FAIL：遗留 ' + str(left)}")
    cleanup(sleeps + shell_pids)


if __name__ == "__main__":
    binary = sys.argv[1]
    print(f"目标二进制: {binary}")
    for scenario in (scenario_shutdown, scenario_suspend, scenario_eof_interrupt, scenario_stdin_eof):
        try:
            scenario(binary)
        except Exception as exc:  # noqa: BLE001 - 探针脚本，收集所有场景结果
            print(f"  !! 场景 {scenario.__name__} 探针异常: {type(exc).__name__}: {exc}")
