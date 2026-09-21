import base64
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from app.service.terminal.errors import TerminalWorkerSignalFailedError
from app.service.terminal.shell_resolver import ShellSpec
from app.service.terminal.worker import ProcessTerminalWorker


def _worker_path() -> Path:
    configured = os.environ.get("CODING_AGENT_TERMINAL_WORKER")
    if configured:
        return Path(configured)
    return (
        Path(__file__).resolve().parents[3]
        / "apps"
        / "terminal-worker"
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
    )


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
            worker.write(b"echo terminal-worker-smoke\r\nexit\r\n")
        else:
            worker.write(b"printf 'terminal-worker-smoke\\n'; exit\n")
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
    assert handshake["protocol"] == "terminal-worker-v2"
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
    )
    worker = ProcessTerminalWorker(str(_worker_path()), "process-tree-integration-test")
    try:
        worker.start(spec, str(tmp_path), on_event)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child_pid is None:
            output = b"".join(
                base64.b64decode(event["data_base64"])
                for event in events
                if event.get("type") == "output"
                and isinstance(event.get("data_base64"), str)
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
    )
    worker = ProcessTerminalWorker(str(_worker_path()), "suspend-close-integration-test")
    try:
        worker.start(spec, str(tmp_path), on_event)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child_pid is None:
            output = b"".join(
                base64.b64decode(event["data_base64"])
                for event in events
                if event.get("type") == "output"
                and isinstance(event.get("data_base64"), str)
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
