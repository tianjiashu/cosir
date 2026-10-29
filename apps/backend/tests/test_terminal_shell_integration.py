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
from terminal_test_support import capture_terminal_effects, geometry, session_entries

from app.config.constant import Constant
from app.tools.terminal_runtime.inputs import TerminalWait
from app.tools.terminal_runtime.session_service import TerminalSessionService

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
    capture_terminal_effects(monkeypatch)
    service = TerminalSessionService()
    runtime = service.start(
        task_id=1, workspace_root=str(tmp_path), shell=shell,
        created_by="agent", run_id=1,
    )
    runtime.wait_for_integration()  # 与 terminal_start 工具一致
    meta = runtime.snapshot()
    session_id = runtime.session_id
    try:
        yield _Terminal(service, session_id, meta)
    finally:
        service.shutdown()


class _Terminal:
    def __init__(self, service: TerminalSessionService, session_id: str, meta: Any) -> None:
        self.service = service
        self.session_id = session_id
        self.meta = meta
        self._ops = 0

    def write(self, input: dict[str, Any], **wait: Any) -> Any:
        self._ops += 1
        return self.service.require(self.session_id, 1).write(
            input, f"op{self._ops}", TerminalWait(**wait) if wait else None, run_id=1,
            tool_call_id=f"call-w{self._ops}", is_cancelled=lambda: False,
        )

    def line(self, text: str, **wait: Any) -> Any:
        return self.write({"type": "line", "text": text},
                          **(wait or {"until": "command_returned", "timeout_ms": 10000}))

    def read(self, **kwargs: Any) -> Any:
        return self.service.require(self.session_id, 1).read(is_cancelled=lambda: False, **kwargs)


def test_integration_ready_and_command_output_range(terminal: _Terminal) -> None:
    assert "shell integration" not in terminal.meta.summary, terminal.meta
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


def test_full_screen_app_screen_view_text_input_and_stable_line_numbers(
    terminal: _Terminal,
) -> None:
    if os.name == "nt" or shutil.which("less") is None:
        pytest.skip("less is not available")
    terminal.line("echo before-less")
    marker = next(
        line for line in terminal.read(tail=10).text.splitlines() if line.endswith("│before-less")
    )
    number = int(marker.split("│")[0])
    opened = terminal.write({"type": "line", "text": "seq 1 200 | less"},
                            until="match", pattern="^1$", timeout_ms=5000)
    assert opened.screen and "s1│1" in opened.text, opened.text
    # 24 行终端里 less 显示 23 行：下移 3 行后才会出现 26。
    scrolled = terminal.write({"type": "keys", "keys": ["down", "down", "down"]},
                              until="match", pattern="^26$", timeout_ms=5000)
    assert scrolled.screen and "s1│4" in scrolled.text, scrolled.text
    # 全屏期间行号不被屏幕行占用：进入前的输出仍在原行号上。
    assert terminal.read(start_line=number, limit=1).text.splitlines()[-1] == marker
    closed = terminal.write({"type": "text", "text": "q"},
                            until="command_returned", timeout_ms=5000)
    assert closed.wait_result["end_reason"] == "command_returned", closed.text
    assert not closed.screen and "s1│" not in closed.text
    assert terminal.read(start_line=number, limit=1).text.splitlines()[-1] == marker


def test_match_and_keys_and_interrupt(terminal: _Terminal) -> None:
    started = terminal.write(
        {"type": "line", "text": "sleep 0.3; echo READY-NOW; sleep 30"},
        until="match", pattern="READY-NOW$", timeout_ms=10000,
    )
    assert started.wait_result["end_reason"] == "matched", started.text
    # 命令执行中投影 commandRunning，前端据此决定关闭时是否确认。
    assert session_entries(terminal.service, 1)[0]["commandRunning"] is True
    # bash 3.2 在两条简单命令之间（DEBUG trap 内）收到 Ctrl-C 时偶发不报告返回（WP1 已知竞态），
    # 等 sleep 真正开始再中断。
    time.sleep(0.3)
    interrupted = terminal.write({"type": "keys", "keys": ["ctrl_c"]},
                                 until="command_returned", timeout_ms=5000)
    assert interrupted.wait_result["end_reason"] == "command_returned", interrupted.text
    assert session_entries(terminal.service, 1)[0]["commandRunning"] is False
    # 历史调出上一条命令：up + enter 重新执行 echo。
    terminal.line("echo recall-me")
    recalled = terminal.write({"type": "keys", "keys": ["up", "enter"]},
                              until="command_returned", timeout_ms=5000)
    assert recalled.text.count("recall-me") >= 1, recalled.text


def test_user_raw_input_lease_and_resize(terminal: _Terminal) -> None:
    service, session_id = terminal.service, terminal.session_id
    assert service.require(session_id, 1).user_input(b"echo typed-by-user\r") == "accepted"
    assert session_entries(service, 1)[0]["inputHolder"] == "user"
    seen = terminal.read(wait=TerminalWait(until="match", pattern="^typed-by-user$",
                                           timeout_ms=5000))
    assert seen.wait_result["end_reason"] == "matched"
    assert "user typed lines" in seen.text
    service.require(session_id, 1).release_user_input()
    assert session_entries(service, 1)[0]["inputHolder"] is None
    service.require(session_id, 1).resize(30, 100)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if geometry(service, session_id) == (30, 100):
            break
        time.sleep(0.05)
    assert geometry(service, session_id) == (30, 100)
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
