"""Windows：``execute_terminal`` 的命令经 worker 执行模式运行时，Job Object 清掉它启动的全部进程，
命令进程不带控制台窗口（``CREATE_NO_WINDOW``）。POSIX 的同类行为见 ``test_local_backend_process_cleanup``。

孙进程用 PowerShell 的 ``Start-Process`` 起一个 ``ping``：它不随命令退出，也不握着命令的输出管道，只有作业
清理能结束它。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.process_tree_support import alive, kill, survivors
from tests.terminal_test_support import requires_terminal_worker, run_local_command

pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="Windows Job Object and console behaviour"),
    requires_terminal_worker,
]

# 起一个 60 秒的 ping 并报出它的 PID；不用双引号，免得经过命令行转义。
_SPAWN_GRANDCHILD = (
    "$p = Start-Process -FilePath ping.exe -ArgumentList '-n','60','127.0.0.1' "
    "-WindowStyle Hidden -PassThru; Write-Output ('GRANDCHILD ' + $p.Id)"
)


def _powershell(command: str, cwd: Path, timeout: float) -> tuple[Any, Any]:
    return run_local_command(command, cwd, timeout, shell="powershell")  # noqa: S604 - 被测的就是显式 shell


def _grandchild(display: str) -> int:
    return int(display.split("GRANDCHILD", 1)[1].split()[0])


def _cleanup(pids: list[int]) -> None:
    for pid in pids:
        kill(pid)


def test_processes_started_by_a_finished_command_are_cleaned(tmp_path: Path) -> None:
    """目的：命令自己结束后，它用 Start-Process 起的进程也被作业清理。
    潜在缺陷：Job Object 没挂上（或挂上前就派生了孙进程），ping 成为孤儿继续跑满 60 秒。"""

    result, output = _powershell(_SPAWN_GRANDCHILD, tmp_path, 30)

    assert result.exit_code == 0, output.display
    pid = _grandchild(output.display)
    left = survivors([pid], timeout=5)
    _cleanup(left)
    assert left == [], "process started by the command survived"


def test_timeout_stops_the_command_and_its_processes(tmp_path: Path) -> None:
    """目的：超时停止整棵作业，不只是命令本身。"""

    started = time.monotonic()
    result, output = _powershell(f"{_SPAWN_GRANDCHILD}; Start-Sleep -Seconds 60", tmp_path, 5)

    assert result.end == "timed_out", output.display
    assert time.monotonic() - started < 20
    pid = _grandchild(output.display)
    left = survivors([pid], timeout=5)
    _cleanup(left)
    assert left == [], "process started by the timed-out command survived"


def test_command_processes_are_cleaned_when_the_backend_is_killed(tmp_path: Path) -> None:
    """目的：后端被强杀时，worker 读到 stdin EOF 结束作业。
    潜在缺陷：worker 只在后端要求时才清理，强杀后命令与它起的进程都成为孤儿。"""

    pid_file = tmp_path / "grandchild.pid"
    command = (
        "$p = Start-Process -FilePath ping.exe -ArgumentList '-n','60','127.0.0.1' "
        f"-WindowStyle Hidden -PassThru; Set-Content -Path '{pid_file}' -Value $p.Id; Start-Sleep -Seconds 60"
    )
    script = f"""
import time
from pathlib import Path
from typing import Any
from app.tools.terminal_runtime.command.local_backend import LocalExecutionBackend
from app.tools.terminal_runtime.command.command_output import CommandOutput
output = CommandOutput(workspace_root=Path({str(tmp_path)!r}), model_budget_chars=20_000)
LocalExecutionBackend().execute({command!r}, {str(tmp_path)!r}, 120, output, shell="powershell")
time.sleep(60)
"""
    backend = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text(encoding="utf-8-sig").strip()):
            assert backend.poll() is None, "backend exited early"
            assert time.monotonic() < deadline, "grandchild pid was not written"
            time.sleep(0.1)
        pid = int(pid_file.read_text(encoding="utf-8-sig").strip())
        assert alive(pid)

        kill(backend.pid)
        backend.wait(10)

        left = survivors([pid], timeout=10)
        _cleanup(left)
        assert left == [], "process started by the command survived the backend"
    finally:
        if backend.poll() is None:
            backend.kill()


def test_command_runs_without_a_console_window(tmp_path: Path) -> None:
    """目的：命令以 ``CREATE_NO_WINDOW`` 启动：有控制台但没有窗口，用户不会看到 cmd 窗口一闪。
    潜在缺陷：去掉这个标志后，每条命令都会弹出（或闪一下）控制台窗口；这里 GetConsoleWindow 会返回非 0。"""

    # C# 的字符串字面量要双引号：用 [char]34 拼出来，命令行上就不必转义引号。
    probe = (
        "$q = [char]34; Add-Type -Namespace Probe -Name Console -MemberDefinition "
        "('[System.Runtime.InteropServices.DllImport(' + $q + 'kernel32.dll' + $q + ')] "
        "public static extern System.IntPtr GetConsoleWindow();'); "
        "Write-Output ('CONSOLE_WINDOW ' + [Probe.Console]::GetConsoleWindow())"
    )

    result, output = _powershell(probe, tmp_path, 60)

    assert result.exit_code == 0, output.display
    assert "CONSOLE_WINDOW 0" in output.display, output.display
