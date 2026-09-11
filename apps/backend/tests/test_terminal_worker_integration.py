import base64
import os
import shutil
import threading
from pathlib import Path

import pytest

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
        worker.start(_shell_spec(), str(tmp_path), 80, 24, on_event)
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
    assert handshake["protocol"] == "terminal-worker-v1"
    assert handshake["instance_id"] == "integration-test-instance"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell smoke test is Windows-only")
def test_real_worker_powershell_command_exits(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    exited = threading.Event()

    def on_event(event: dict[str, object]) -> None:
        events.append(event)
        if event.get("type") == "exit":
            exited.set()

    worker = ProcessTerminalWorker(str(_worker_path()), "powershell-integration-test")
    try:
        worker.start(_powershell_command_spec(), str(tmp_path), 80, 24, on_event)
        assert exited.wait(10), f"worker did not report PowerShell exit: {events!r}"
    finally:
        worker.close()

    output = b"".join(
        base64.b64decode(event["data_base64"])
        for event in events
        if event.get("type") == "output" and isinstance(event.get("data_base64"), str)
    )
    assert b"terminal-worker-powershell-smoke" in output
