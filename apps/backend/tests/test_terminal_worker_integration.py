import base64
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from app.service.terminal.errors import TerminalWorkerSignalFailedError
from app.service.terminal.process_terminal_worker import ProcessTerminalWorker
from app.service.terminal.shell_resolver import ShellSpec


def _worker_path() -> Path:
    configured = os.environ.get("CODING_AGENT_TERMINAL_WORKER")
    if configured:
        return Path(configured)
    return (
        Path(__file__).resolve().parents[3]
        / "target"
        / "debug"
        / ("terminal-worker.exe" if os.name == "nt" else "terminal-worker")
    )


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


@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
def test_real_worker_round_trip_and_exit(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    worker = ProcessTerminalWorker(str(_worker_path()), "integration-test-instance")
    try:
        worker.start(_shell_spec(), str(tmp_path), on_event)
        if os.name == "nt":
            worker.write({"type": "line", "text": "echo terminal-worker-smoke"})
            worker.write({"type": "line", "text": "exit"})
        else:
            worker.write({"type": "line", "text": "printf 'terminal-worker-smoke\\n'; exit"})
        assert exited.wait(10), f"worker did not report shell exit: {events!r}"
    finally:
        worker.close()

    output = b"".join(
        base64.b64decode(event["data_base64"])
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
    )
    assert b"terminal-worker-smoke" in output
    handshake = next(event for event in events if event.get("type") == "handshake")
    assert handshake["instance_id"] == "integration-test-instance"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell smoke test is Windows-only")
@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
def test_real_worker_powershell_command_exits(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    worker = ProcessTerminalWorker(str(_worker_path()), "powershell-integration-test")
    try:
        worker.start(_powershell_command_spec(), str(tmp_path), on_event)
        assert exited.wait(10), f"worker did not report PowerShell exit: {events!r}"
    finally:
        worker.close()

    output = b"".join(
        base64.b64decode(event["data_base64"])
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
    )
    assert b"terminal-worker-powershell-smoke" in output


@pytest.mark.skipif(os.name == "nt", reason="Unix process tree test")
@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
def test_real_worker_close_kills_shell_descendants(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    child_pid: int | None = None

    def on_event(event: dict[str, object]) -> None:
        events.append(event)

    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    spec = ShellSpec(
        Path(executable).name,
        executable,
        (executable, "-c", "sleep 60 & child=$!; echo CHILD_PID:$child; wait"),
        integration="off",
    )
    worker = ProcessTerminalWorker(str(_worker_path()), "process-tree-integration-test")
    try:
        worker.start(spec, str(tmp_path), on_event)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child_pid is None:
            output = b"".join(
                base64.b64decode(event["data_base64"])
                for event in events
                if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
            ).decode("utf-8", errors="replace")
            marker = "CHILD_PID:"
            if marker in output:
                child_pid = int(output.split(marker, 1)[1].split()[0])
                break
            time.sleep(0.05)
        assert child_pid is not None, f"child pid was not reported: {events!r}"
    finally:
        worker.close()

    assert child_pid is not None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"shell descendant {child_pid} survived worker close")


@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
def test_real_worker_signal_failure_is_reported_to_caller(tmp_path: Path) -> None:
    worker = ProcessTerminalWorker(str(_worker_path()), "signal-integration-test")
    try:
        worker.start(_shell_spec(), str(tmp_path), lambda event: None)
        with pytest.raises(TerminalWorkerSignalFailedError):
            worker.signal("unknown-signal")
    finally:
        worker.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY interrupt test")
@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
@pytest.mark.parametrize("shell_name", ["cmd", "powershell", "pwsh", "custom_cmd"])
def test_windows_ctrl_c_is_delivered_and_isolated_per_conpty_session(
    tmp_path: Path,
    shell_name: str,
) -> None:
    first_events: list[dict[str, object]] = []
    second_events: list[dict[str, object]] = []
    first = ProcessTerminalWorker(str(_worker_path()), f"interrupt-{shell_name}-first")
    second = ProcessTerminalWorker(str(_worker_path()), f"interrupt-{shell_name}-second")

    def output_bytes(events: list[dict[str, object]]) -> bytes:
        return b"".join(
            base64.b64decode(event["data_base64"])
            for event in events
            if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
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
        first.write({"type": "line", "text": ready_command})
        second.write({"type": "line", "text": ready_command})
        assert wait_for_output(first_events, ready_marker, 5), "first shell did not become ready"
        assert wait_for_output(second_events, ready_marker, 5), "second shell did not become ready"

        if shell.kind in {"powershell", "pwsh"}:
            command = "Start-Sleep -Seconds 90"
            echo = "Write-Output"
        else:
            command = "ping -n 90 127.0.0.1 >nul"
            echo = "echo"
        first.write({"type": "line", "text": command})
        second.write({"type": "line", "text": command})
        first_recovered = f"{echo} FIRST_SESSION_RECOVERED"
        second_recovered = f"{echo} SECOND_SESSION_RECOVERED"
        first_recovered_marker = b"FIRST_SESSION_RECOVERED"
        second_recovered_marker = b"SECOND_SESSION_RECOVERED"

        time.sleep(1)

        prompt = b"PS " if shell.kind in {"powershell", "pwsh"} else b">"
        first_output_offset = len(output_bytes(first_events))
        second_output_offset = len(output_bytes(second_events))
        first.signal("interrupt")
        assert wait_for_prompt_after(
            first_events, first_output_offset, prompt, 5
        ), "Ctrl+C did not return the first shell to its prompt"
        first.write({"type": "line", "text": first_recovered})
        assert wait_for_output(first_events, first_recovered_marker, 5), (
            "Ctrl+C did not return the first shell to a usable prompt: "
            f"{output_bytes(first_events)[-2000:]!r}"
        )
        assert not wait_for_prompt_after(
            second_events, second_output_offset, prompt, 0.5
        ), "interrupting the first ConPTY session also interrupted the second session"

        second.signal("interrupt")
        assert wait_for_prompt_after(
            second_events, second_output_offset, prompt, 5
        ), "Ctrl+C did not return the second shell to its prompt"
        second.write({"type": "line", "text": second_recovered})
        assert wait_for_output(
            second_events, second_recovered_marker, 5
        ), "the second shell did not recover when its own Ctrl+C was sent"
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix PTY suspend semantics")
@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
def test_real_worker_close_reaps_a_suspended_job(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    child_pid: int | None = None

    def on_event(event: dict[str, object]) -> None:
        events.append(event)

    executable = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    spec = ShellSpec(
        Path(executable).name,
        executable,
        (executable, "-c", "sleep 60 & child=$!; echo STOPPED_CHILD_PID:$child; wait"),
        integration="off",
    )
    worker = ProcessTerminalWorker(str(_worker_path()), "suspend-close-integration-test")
    try:
        worker.start(spec, str(tmp_path), on_event)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child_pid is None:
            output = b"".join(
                base64.b64decode(event["data_base64"])
                for event in events
                if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
            ).decode("utf-8", errors="replace")
            marker = "STOPPED_CHILD_PID:"
            if marker in output:
                child_pid = int(output.split(marker, 1)[1].split()[0])
                break
            time.sleep(0.05)
        assert child_pid is not None, f"child pid was not reported: {events!r}"
        worker.signal("suspend")
    finally:
        worker.close()

    assert child_pid is not None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"suspended job {child_pid} survived worker close")


@pytest.mark.skipif(not _worker_path().is_file(), reason="terminal-worker binary is not built")
@pytest.mark.asyncio
async def test_task_shell_state_survives_run_completion_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 PTY 经工具创建后，下一 Run 发现并继续使用原 shell 环境。"""
    import json

    from app.config.constant import Constant
    from app.core.tools.schemas import ToolExecutionContext
    from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
    from app.core.tools.tool_handler.terminal_session import (
        TerminalListTool,
        TerminalReadTool,
        TerminalStartTool,
        TerminalWriteTool,
    )
    from app.core.workflows.react.workflow import ReactLikeWorkflow
    from app.service.terminal.terminal_session_service import TerminalSessionService

    # worker 路径只有宿主环境变量一个来源，测试沿用同一配置边界。
    monkeypatch.setenv(Constant.Terminal.WORKER_ENV, str(_worker_path()))
    terminal = TerminalSessionService(status_observer=lambda _change: None)
    cancelled_runs: set[int] = set()
    dependencies = ToolRuntimeDependencies(
        terminal_session_service=terminal, is_run_cancelled=lambda run: run in cancelled_runs
    )
    first = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=1,
        runtime_dependencies=dependencies,
    )
    second = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=2,
        runtime_dependencies=dependencies,
    )
    try:
        started = TerminalStartTool().execute(
            shell="cmd" if os.name == "nt" else "bash",
            label="persistent shell",
            execution_context=first,
        )
        assert started.status == "success", started.content
        session_id = str(started.display_data["session_id"])
        set_command = (
            "set COSIR_TERMINAL_STATE=kept"
            if os.name == "nt"
            else "export COSIR_TERMINAL_STATE=kept"
        )
        assert (
            TerminalWriteTool()
            .execute(
                session_id=session_id,
                operation_id="set-env",
                input={"type": "line", "text": set_command},
                wait_ms=0,
                execution_context=first,
            )
            .status
            == "success"
        )
        await ReactLikeWorkflow().finalize_run(1)
        cancelled_runs.add(1)
        assert TerminalReadTool().execute(session_id, execution_context=first).status == "cancelled"
        listed = TerminalListTool().execute(execution_context=second)
        payload = json.loads(listed.content.split("\n", 1)[1])
        assert payload["sessions"][0]["session_id"] == session_id
        assert payload["sessions"][0]["status"] == "running"
        command = (
            "echo STATE:%COSIR_TERMINAL_STATE%"
            if os.name == "nt"
            else 'printf "STATE:%s\\n" "$COSIR_TERMINAL_STATE"'
        )
        TerminalWriteTool().execute(
            session_id=session_id,
            operation_id="check-env",
            input={"type": "line", "text": command},
            wait_ms=0,
            execution_context=second,
        )
        output = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "STATE:kept" not in output:
            result = terminal.read(session_id, task_id=1, run_id=1, wait_ms=3000)
            output = result["history"]
            if "screen" in result:
                output += result["screen"]["text"]
        assert "STATE:kept" in output
    finally:
        terminal.shutdown()
    assert terminal.list_sessions(task_id=1, workspace_id=1) == []
