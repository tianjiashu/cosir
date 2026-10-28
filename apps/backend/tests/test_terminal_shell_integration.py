"""真实 PTY 集成：zsh 与 bash（/bin/bash 3.2、Homebrew 5.x）的命令区间、条件等待、按键、原始输入、
resize 与输入租约；Windows PowerShell / pwsh 只在 Windows 宿主运行。未安装的 shell 显式 skip。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from app.config.constant import Constant
from app.tools.terminal_runtime.terminal_input import TerminalWait
from app.tools.terminal_runtime.terminal_session_service import TerminalSessionService

_ROOT = Path(__file__).resolve().parents[3]
_WORKER = _ROOT / "target" / "debug" / ("terminal-worker.exe" if os.name == "nt" else "terminal-worker")
_UNIX_SHELLS = ["zsh", "/bin/bash", "/opt/homebrew/bin/bash"]
_WINDOWS_SHELLS = ["powershell", "pwsh"]


def _available(shell: str) -> bool:
    return Path(shell).is_file() if shell.startswith("/") else shutil.which(shell) is not None


@pytest.fixture(params=_WINDOWS_SHELLS if os.name == "nt" else _UNIX_SHELLS)
def terminal(request: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    shell = request.param
    if not _available(shell):
        pytest.skip(f"{shell} is not installed")
    if not _WORKER.is_file():
        pytest.skip("terminal-worker must be built")
    monkeypatch.setenv(Constant.Terminal.WORKER_ENV, str(_WORKER))
    # 用户配置可能很慢或吞掉集成，测试使用干净的 HOME。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    service = TerminalSessionService(on_task_sessions_changed=lambda _task: None)
    meta = service.start(
        task_id=1, workspace_root=str(tmp_path), shell=shell,
        created_by="agent", run_id=1,
    )
    session_id = str(meta["session_id"])
    try:
        yield _Terminal(service, session_id, meta)
    finally:
        service.shutdown()


class _Terminal:
    def __init__(self, service: TerminalSessionService, session_id: str, meta: dict) -> None:
        self.service = service
        self.session_id = session_id
        self.meta = meta
        self._ops = 0

    def write(self, input: dict[str, Any], **wait: Any) -> Any:
        self._ops += 1
        return self.service.write(
            self.session_id, task_id=1, operation_id=f"op{self._ops}", input=input,
            wait=TerminalWait(**wait) if wait else None, run_id=1, is_cancelled=lambda: False,
        )

    def line(self, text: str, **wait: Any) -> Any:
        return self.write({"type": "line", "text": text},
                          **(wait or {"until": "command_returned", "timeout_ms": 10000}))

    def read(self, **kwargs: Any) -> Any:
        return self.service.read(self.session_id, task_id=1, run_id=1,
                                 is_cancelled=lambda: False, **kwargs)


def test_integration_ready_and_command_output_range(terminal: _Terminal) -> None:
    assert terminal.meta["integration"]["status"] == "ready", terminal.meta
    result = terminal.line("printf 'alpha\\nbeta\\n'")
    assert result.wait_result["end_reason"] == "command_returned", result.text
    header = result.text.splitlines()[1]
    assert "exit 0" in header and "output lines" in header, result.text
    start, end = (int(n) for n in header.split("output lines ")[1].split(" ")[0].split("-"))
    ranged = terminal.read(start_line=start, limit=end - start + 1)
    assert [line.split("│", 1)[1] for line in ranged.text.splitlines()[-2:]] == ["alpha", "beta"]
    failed = terminal.line("false")
    assert "exit 1" in failed.text and "no output" in failed.text


def test_line_numbers_survive_scroll_and_clear(terminal: _Terminal) -> None:
    terminal.line("for i in $(seq 1 60); do echo row$i; done")
    numbered = terminal.read(tail=400)
    row1 = next(line for line in numbered.text.splitlines() if line.endswith("│row1"))
    number = int(row1.split("│")[0])
    terminal.line("clear")
    again = terminal.read(start_line=number, limit=1)
    assert again.text.splitlines()[-1] == f"{number}│row1"


def test_long_wrapped_line_is_one_logical_line(terminal: _Terminal) -> None:
    text = "x" * 200
    result = terminal.line(f"echo {text}")
    assert any(line.endswith(f"│{text}") for line in result.text.splitlines()), result.text


def test_match_and_keys_and_interrupt(terminal: _Terminal) -> None:
    started = terminal.write(
        {"type": "line", "text": "sleep 0.3; echo READY-NOW; sleep 30"},
        until="match", pattern="READY-NOW$", timeout_ms=10000,
    )
    assert started.wait_result["end_reason"] == "matched", started.text
    # 命令执行中投影 commandRunning，前端据此决定关闭时是否确认。
    assert terminal.service.project_task_sessions(1)[0]["commandRunning"] is True
    # bash 3.2 在两条简单命令之间（DEBUG trap 内）收到 Ctrl-C 时偶发不报告返回（WP1 已知竞态），
    # 等 sleep 真正开始再中断。
    time.sleep(0.3)
    interrupted = terminal.write({"type": "keys", "keys": ["ctrl_c"]},
                                 until="command_returned", timeout_ms=5000)
    assert interrupted.wait_result["end_reason"] == "command_returned", interrupted.text
    assert terminal.service.project_task_sessions(1)[0]["commandRunning"] is False
    # 历史调出上一条命令：up + enter 重新执行 echo。
    terminal.line("echo recall-me")
    recalled = terminal.write({"type": "keys", "keys": ["up", "enter"]},
                              until="command_returned", timeout_ms=5000)
    assert recalled.text.count("recall-me") >= 1, recalled.text


def test_user_raw_input_lease_and_resize(terminal: _Terminal) -> None:
    service, session_id = terminal.service, terminal.session_id
    assert service.user_input(session_id, task_id=1, data=b"echo typed-by-user\r") == "accepted"
    assert service.project_task_sessions(1)[0]["inputHolder"] == "user"
    seen = terminal.read(wait=TerminalWait(until="match", pattern="^typed-by-user$",
                                           timeout_ms=5000))
    assert seen.wait_result["end_reason"] == "matched"
    assert "user typed lines" in seen.text
    service.release_user_input(session_id, task_id=1)
    assert service.project_task_sessions(1)[0]["inputHolder"] is None
    service.resize(session_id, task_id=1, rows=30, cols=100)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if service.list_sessions(task_id=1)[0]["geometry"] == {"rows": 30, "columns": 100}:
            break
        time.sleep(0.05)
    assert service.list_sessions(task_id=1)[0]["geometry"] == {"rows": 30, "columns": 100}
    size = terminal.line("stty size")
    assert any(line.endswith("│30 100") for line in size.text.splitlines()), size.text


def test_quiet_wait_without_command_return(terminal: _Terminal) -> None:
    result = terminal.write({"type": "line", "text": "cat"}, until="output_quiet",
                            quiet_ms=300, timeout_ms=5000)
    assert result.wait_result["end_reason"] == "output_quiet"
    assert "executing `cat`" in result.text
    done = terminal.write({"type": "signal", "signal": "eof"} if os.name != "nt"
                          else {"type": "keys", "keys": ["ctrl_c"]},
                          until="command_returned", timeout_ms=5000)
    assert done.wait_result["end_reason"] == "command_returned", done.text
