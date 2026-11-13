import base64
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker, ShellSpec
from tests.terminal_test_support import requires_terminal_worker


def _shell_spec() -> ShellSpec:
    if os.name == "nt":
        executable = shutil.which("cmd.exe") or "cmd.exe"
        return ShellSpec("cmd", executable, (executable, "/Q", "/D"))
    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return ShellSpec(Path(executable).name, executable, (executable,))


def _powershell_command_spec() -> ShellSpec:
    executable = shutil.which("powershell.exe") or "powershell.exe"
    return ShellSpec(
        "powershell",
        executable,
        (
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Write-Output terminal-worker-powershell-smoke",
        ),
        integration="off",
    )


def _windows_interactive_shell_spec(name: str) -> ShellSpec:
    if name in {"cmd", "custom_cmd"}:
        executable = shutil.which("cmd.exe") or os.environ.get("COMSPEC")
        if not executable:
            pytest.skip("cmd.exe is not available")
        kind = "cmd" if name == "cmd" else "custom"
        return ShellSpec(kind, executable, (executable, "/Q", "/D"))
    executable_name = "powershell.exe" if name == "powershell" else "pwsh.exe"
    executable = shutil.which(executable_name)
    if not executable:
        pytest.skip(f"{executable_name} is not available")
    return ShellSpec(name, executable, (executable, "-NoLogo", "-NoProfile"))


def _raw(worker: ProcessTerminalWorker, data: bytes) -> None:
    """用户的原始按键：与终端 WS 的 raw 帧同一协议（Agent 不再有 line / keys 输入）。"""
    worker.write({"type": "raw", "data_base64": base64.b64encode(data).decode("ascii")})


def _line(worker: ProcessTerminalWorker, text: str) -> None:
    _raw(worker, (text + "\r").encode())


def _output_text(events: list[dict[str, object]]) -> str:
    return b"".join(
        event["data"]
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data"), bytes)
    ).decode("utf-8", errors="replace")


def _wait_reported_pids(events: list[dict[str, object]], marker: str) -> list[int]:
    """等 shell 打印 ``<marker><pid>[:<pid>...]``，返回其中的 pid。"""

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        output = _output_text(events)
        if marker in output:
            return [int(pid) for pid in output.split(marker, 1)[1].split()[0].split(":")]
        time.sleep(0.05)
    pytest.fail(f"pids were not reported: {events!r}")


def _assert_gone(pids: list[int], what: str, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if _is_alive(pid)]
        time.sleep(0.05)
    if alive:
        for pid in alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        pytest.fail(f"{what} {alive} survived")


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@requires_terminal_worker
def test_real_worker_round_trip_and_exit(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    worker = ProcessTerminalWorker("integration-test-instance")
    try:
        worker.start(_shell_spec(), str(tmp_path), on_event)
        if os.name == "nt":
            _line(worker, "echo terminal-worker-smoke")
            _line(worker, "exit")
        else:
            _line(worker, "printf 'terminal-worker-smoke\\n'; exit")
        assert exited.wait(10), f"worker did not report shell exit: {events!r}"
    finally:
        worker.close()

    output = b"".join(
        event["data"]
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data"), bytes)
    )
    assert b"terminal-worker-smoke" in output
    handshake = next(event for event in events if event.get("type") == "handshake")
    assert handshake["instance_id"] == "integration-test-instance"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell smoke test is Windows-only")
@requires_terminal_worker
def test_real_worker_powershell_command_exits(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    worker = ProcessTerminalWorker("powershell-integration-test")
    try:
        worker.start(_powershell_command_spec(), str(tmp_path), on_event)
        assert exited.wait(10), f"worker did not report PowerShell exit: {events!r}"
    finally:
        worker.close()

    output = b"".join(
        event["data"]
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data"), bytes)
    )
    assert b"terminal-worker-powershell-smoke" in output


@pytest.mark.skipif(os.name == "nt", reason="Unix process tree test")
@requires_terminal_worker
def test_real_worker_close_kills_shell_descendants(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    spec = ShellSpec(
        Path(executable).name,
        executable,
        (executable, "-c", "sleep 60 & child=$!; echo CHILD_PID:$child; wait"),
        integration="off",
    )
    worker = ProcessTerminalWorker("process-tree-integration-test")
    try:
        worker.start(spec, str(tmp_path), events.append)
        pids = _wait_reported_pids(events, "CHILD_PID:")
    finally:
        worker.close()

    _assert_gone(pids, "shell descendant")


@pytest.mark.skipif(os.name == "nt", reason="Unix session cleanup test")
@requires_terminal_worker
def test_real_worker_reaps_jobs_left_by_a_shell_that_exits_by_itself(tmp_path: Path) -> None:
    """shell 自己退出后已不在进程树上，它留下的忽略 HUP 的进程仍按会话归属被清理。

    没有作业控制的 ``sh -c`` 里，普通 ``&`` 进程与 shell 同在前台进程组，shell 退出时内核的 SIGHUP
    会顺带结束它们；真正会残留的是 ``nohup`` 与 ``trap '' HUP``。等 0.3 秒再退出，让 nohup 来得及
    忽略 HUP，否则用例会被这次 SIGHUP 碰巧通过。
    """

    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    spec = ShellSpec(
        "sh",
        "/bin/sh",
        (
            "/bin/sh",
            "-c",
            "nohup sleep 60 >/dev/null 2>&1 & a=$!; (trap '' HUP; exec sleep 60) & b=$!; "
            "sleep 0.3; echo JOB_PIDS:$a:$b",
        ),
        integration="off",
    )
    worker = ProcessTerminalWorker("shell-exit-integration-test")
    try:
        worker.start(spec, str(tmp_path), on_event)
        pids = _wait_reported_pids(events, "JOB_PIDS:")
        assert exited.wait(10), f"worker did not report shell exit: {events!r}"
    finally:
        worker.close()

    _assert_gone(pids, "job left by the exited shell")


@pytest.mark.skipif(os.name == "nt", reason="Unix process group kill test")
@requires_terminal_worker
def test_worker_cleans_up_when_the_backend_process_group_is_killed(tmp_path: Path) -> None:
    """宿主强杀后端时对后端整个进程组发 SIGKILL：worker 自成会话不被波及，读到 stdin EOF 后
    清理会话。

    会话进程用 nohup 启动：worker 若被一起杀掉，PTY 关闭时内核的 SIGHUP 会结束普通进程，用例就测
    不出 worker 有没有清理。
    """

    job_file = tmp_path / "job.pid"
    script = f"""
import threading, time
from app.tools.terminal_runtime.worker_client import ShellSpec
from app.tools.terminal_runtime.worker_client import ProcessTerminalWorker

handshake = threading.Event()
worker_pid = []
def on_event(event):
    if event.get("type") == "handshake":
        worker_pid.append(event["pid"])
        handshake.set()
command = "nohup sleep 60 >/dev/null 2>&1 & echo $! > {job_file}; wait"
spec = ShellSpec("sh", "/bin/sh", ("/bin/sh", "-c", command), integration="off")
ProcessTerminalWorker("backend-kill-integration-test").start(spec, {str(tmp_path)!r}, on_event)
handshake.wait(5)
print(f"WORKER_PID:{{worker_pid[0]}}", flush=True)
time.sleep(60)
"""
    backend = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert backend.stdout is not None
        line = backend.stdout.readline()
        assert line.startswith("WORKER_PID:"), line
        worker_pid = int(line.split(":", 1)[1])
        deadline = time.monotonic() + 5
        while not (job_file.exists() and job_file.read_text().strip()):
            assert time.monotonic() < deadline, "job pid was not written"
            time.sleep(0.05)
        job_pid = int(job_file.read_text())
        assert _is_alive(worker_pid) and _is_alive(job_pid)

        os.killpg(backend.pid, signal.SIGKILL)
        backend.wait(5)

        _assert_gone([job_pid], "session job after the backend was killed", timeout=5)
        _assert_gone([worker_pid], "worker after the backend was killed", timeout=5)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(backend.pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY interrupt test")
@requires_terminal_worker
@pytest.mark.parametrize("shell_name", ["cmd", "powershell", "pwsh", "custom_cmd"])
def test_windows_ctrl_c_is_delivered_and_isolated_per_conpty_session(
    tmp_path: Path,
    shell_name: str,
) -> None:
    first_events: list[dict[str, object]] = []
    second_events: list[dict[str, object]] = []
    first = ProcessTerminalWorker(f"interrupt-{shell_name}-first")
    second = ProcessTerminalWorker(f"interrupt-{shell_name}-second")

    def output_bytes(events: list[dict[str, object]]) -> bytes:
        return b"".join(
            event["data"]
            for event in events
            if event.get("type") == "output" and isinstance(event.get("data"), bytes)
        )

    def output_contains(events: list[dict[str, object]], marker: bytes) -> bool:
        return marker in output_bytes(events)

    def wait_for_output(events: list[dict[str, object]], marker: bytes, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if output_contains(events, marker):
                return True
            time.sleep(0.05)
        return output_contains(events, marker)

    def wait_for_prompt_after(
        events: list[dict[str, object]],
        offset: int,
        prompt: bytes,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if prompt in output_bytes(events)[offset:]:
                return True
            time.sleep(0.05)
        return prompt in output_bytes(events)[offset:]

    try:
        shell = _windows_interactive_shell_spec(shell_name)
        first.start(shell, str(tmp_path), first_events.append)
        second.start(shell, str(tmp_path), second_events.append)

        ready_marker = f"{shell_name}_READY".encode()
        ready_command = (
            "Write-Output " if shell.kind in {"powershell", "pwsh"} else "echo "
        ) + ready_marker.decode()
        _line(first, ready_command)
        _line(second, ready_command)
        assert wait_for_output(first_events, ready_marker, 5), "first shell did not become ready"
        assert wait_for_output(second_events, ready_marker, 5), "second shell did not become ready"

        if shell.kind in {"powershell", "pwsh"}:
            command = "Start-Sleep -Seconds 90"
            echo = "Write-Output"
        else:
            command = "ping -n 90 127.0.0.1 >nul"
            echo = "echo"
        _line(first, command)
        _line(second, command)
        first_recovered = f"{echo} FIRST_SESSION_RECOVERED"
        second_recovered = f"{echo} SECOND_SESSION_RECOVERED"
        first_recovered_marker = b"FIRST_SESSION_RECOVERED"
        second_recovered_marker = b"SECOND_SESSION_RECOVERED"

        time.sleep(1)

        prompt = b"PS " if shell.kind in {"powershell", "pwsh"} else b">"
        first_output_offset = len(output_bytes(first_events))
        second_output_offset = len(output_bytes(second_events))
        _raw(first, b"\x03")
        assert wait_for_prompt_after(
            first_events, first_output_offset, prompt, 5
        ), "Ctrl+C did not return the first shell to its prompt"
        _line(first, first_recovered)
        assert wait_for_output(first_events, first_recovered_marker, 5), (
            "Ctrl+C did not return the first shell to a usable prompt: "
            f"{output_bytes(first_events)[-2000:]!r}"
        )
        assert not wait_for_prompt_after(
            second_events, second_output_offset, prompt, 0.5
        ), "interrupting the first ConPTY session also interrupted the second session"

        _raw(second, b"\x03")
        assert wait_for_prompt_after(
            second_events, second_output_offset, prompt, 5
        ), "Ctrl+C did not return the second shell to its prompt"
        _line(second, second_recovered)
        assert wait_for_output(
            second_events, second_recovered_marker, 5
        ), "the second shell did not recover when its own Ctrl+C was sent"
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix PTY suspend semantics")
@requires_terminal_worker
def test_real_worker_close_reaps_a_suspended_job(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    spec = ShellSpec(
        Path(executable).name,
        executable,
        (executable, "-c", "sleep 60 & child=$!; echo STOPPED_CHILD_PID:$child; wait"),
        integration="off",
    )
    worker = ProcessTerminalWorker("suspend-close-integration-test")
    try:
        worker.start(spec, str(tmp_path), events.append)
        pids = _wait_reported_pids(events, "STOPPED_CHILD_PID:")
        _raw(worker, b"\x1a")
    finally:
        worker.close()

    _assert_gone(pids, "suspended job")


@requires_terminal_worker
@pytest.mark.asyncio
async def test_session_shell_state_survives_run_completion_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 PTY 经服务创建后跨 Run 保留：同一 Session 的下一 Run 读到的是同一个 shell 环境。

    Agent 不能执行命令（只能读、列、填入），驱动 shell 的状态变化由用户原始输入完成。
    """
    from terminal_test_support import (
        active_session_ids,
        ignore_session_changes,
        user_enter,
        wait_for_integration,
        wait_until,
    )

    from app.core.tool.schemas import ToolExecutionContext
    from app.core.workflow.cancel.run_cancellation_registry import (
        cancellation_registry,
    )
    from app.service import depends as service_depends
    from app.tools.terminal_runtime.session.session_service import TerminalSessionService
    from app.tools.tool_handler.terminal.terminal_list import TerminalListTool
    from tests.runtime_context_stub import singleton_getter

    terminal = TerminalSessionService(on_sessions_changed=ignore_session_changes)
    monkeypatch.setattr(
        service_depends, "get_terminal_session_service", singleton_getter(lambda: terminal)
    )
    runtime = terminal.start(
        session_id=1,
        cwd=str(tmp_path),
        shell="cmd" if os.name == "nt" else "bash",
    )
    wait_for_integration(runtime)
    terminal_session_id = runtime.terminal_session_id
    second = ToolExecutionContext(
        step_id="step-1", session_id=1, workspace_id=1, workspace_root=tmp_path, run_id=2
    )

    def run_as_user(text: str) -> None:
        """用户敲一行并等这条命令结束：只在命令号前进且不再执行时返回，避免拿到上一条的结果。"""
        before = runtime._observation.command_counter
        user_enter(runtime, text)
        wait_until(
            lambda: (command := runtime._observation.latest_command) is not None
            and command.number > before
            and command.status != "executing",
            timeout=10,
        )

    try:
        set_command = (
            "set COSIR_TERMINAL_STATE=kept"
            if os.name == "nt"
            else "export COSIR_TERMINAL_STATE=kept"
        )
        run_as_user(set_command)
        # 第一个 Run 被停止（取消前的拦截归执行器，不在 handler）；执行释放取走 Session 信号后，同一 Session 的
        # 后续 Run 复用终端不受影响。
        cancellation_registry.request_cancel(1)
        cancellation_registry.consume(1)
        listed = TerminalListTool().execute(execution_context=second)
        session_line = listed.content.split("\n", 1)[0]
        assert session_line.startswith(f"session {terminal_session_id}")
        assert " · running" in session_line
        command = (
            "echo STATE:%COSIR_TERMINAL_STATE%"
            if os.name == "nt"
            else 'printf "STATE:%s\\n" "$COSIR_TERMINAL_STATE"'
        )
        run_as_user(command)
        result = terminal.require(terminal_session_id, 1).read(is_cancelled=lambda: False)
        assert "STATE:kept" in result.text
    finally:
        cancellation_registry.consume(1)
        terminal.shutdown()
    assert active_session_ids(terminal) == []
