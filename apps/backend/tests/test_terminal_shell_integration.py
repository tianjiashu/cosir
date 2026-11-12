"""真实 PTY 集成：zsh 与 bash（/bin/bash 3.2、Homebrew 5.x）的命令区间、条件等待、Agent 填入、
用户原始输入与 resize；Windows PowerShell / pwsh 只在 Windows 宿主运行。未安装的 shell 显式 skip。

Agent 对交互终端只能填入一行、不能执行：驱动 shell 的输入一律走用户原始字节（与终端 WS 同一路径）。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from terminal_test_support import (
    geometry,
    ignore_session_changes,
    session_entries,
    terminal_worker_path,
    user_enter,
    wait_for_integration,
    wait_until,
)

from app.tools.terminal_runtime.observation.wait import TerminalWait
from app.tools.terminal_runtime.session.session_service import TerminalSessionService

_UNIX_SHELLS = ["zsh", "/bin/bash", "/opt/homebrew/bin/bash"]
_WINDOWS_SHELLS = ["powershell", "pwsh"]


def _available(shell: str) -> bool:
    return Path(shell).is_file() if shell.startswith("/") else shutil.which(shell) is not None


@pytest.fixture(params=_WINDOWS_SHELLS if os.name == "nt" else _UNIX_SHELLS)
def terminal(request: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    shell = request.param
    if not _available(shell):
        pytest.skip(f"{shell} is not installed")
    if not terminal_worker_path().is_file():
        pytest.skip("terminal-worker must be built")
    # 用户配置可能很慢或吞掉集成，测试使用干净的 HOME。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    service = TerminalSessionService(on_sessions_changed=ignore_session_changes)
    runtime = service.start(
        task_id=1, cwd=str(tmp_path), shell=shell,
    )
    wait_for_integration(runtime)
    try:
        yield _Terminal(service, runtime)
    finally:
        service.shutdown()


class _Terminal:
    def __init__(self, service: TerminalSessionService, runtime: Any) -> None:
        self.service = service
        self.runtime = runtime
        self.session_id: str = runtime.session_id

    def user_bytes(self, data: bytes) -> None:
        assert self.runtime.user_input(data) == "accepted"

    def user(self, text: str) -> None:
        user_enter(self.runtime, text)

    def prefill(self, command: str) -> Any:
        return self.runtime.prefill(command, run_id=1, tool_call_id="call-prefill")

    def read(self, **kwargs: Any) -> Any:
        return self.runtime.read(is_cancelled=lambda: False, **kwargs)

    def run_user(self, text: str, timeout: float = 10.0) -> Any:
        """用户敲一行并回车，等这条命令结束，返回尾部观察。"""
        before = self.runtime._observation.command_counter
        self.user(text)
        wait_until(
            lambda: (command := self.runtime._observation.latest_command) is not None
            and command.number > before
            and command.status != "executing",
            timeout=timeout,
        )
        return self.read(tail=400)


def test_integration_ready_and_command_output_range(terminal: _Terminal) -> None:
    assert "shell integration" not in terminal.runtime.snapshot().summary
    result = terminal.run_user("printf 'alpha\\nbeta\\n'")
    header = next(line for line in result.text.splitlines() if "output lines" in line)
    assert "exit 0" in header, result.text
    start, end = (int(n) for n in header.split("output lines ")[1].split(" ")[0].split("-"))
    ranged = terminal.read(start_line=start, limit=end - start + 1)
    assert [line.split("│", 1)[1] for line in ranged.text.splitlines()[-2:]] == ["alpha", "beta"]
    failed = terminal.run_user("false")
    assert "exit 1" in failed.text and "no output" in failed.text


def test_line_numbers_survive_scroll_and_clear(terminal: _Terminal) -> None:
    numbered = terminal.run_user("for i in $(seq 1 60); do echo row$i; done")
    row1 = next(line for line in numbered.text.splitlines() if line.endswith("│row1"))
    number = int(row1.split("│")[0])
    terminal.run_user("clear")
    again = terminal.read(start_line=number, limit=1)
    assert again.text.splitlines()[-1] == f"{number}│row1"


def test_long_wrapped_line_is_one_logical_line(terminal: _Terminal) -> None:
    text = "x" * 200
    result = terminal.run_user(f"echo {text}")
    assert any(line.endswith(f"│{text}") for line in result.text.splitlines()), result.text


def test_full_screen_app_screen_view_and_stable_line_numbers(terminal: _Terminal) -> None:
    if os.name == "nt" or shutil.which("less") is None:
        pytest.skip("less is not available")
    terminal.run_user("echo before-less")
    marker = next(
        line for line in terminal.read(tail=10).text.splitlines() if line.endswith("│before-less")
    )
    number = int(marker.split("│")[0])
    terminal.user("seq 1 200 | less")
    opened = terminal.read(wait=TerminalWait(until="match", pattern="^1$", timeout_ms=5000))
    assert opened.wait_result["end_reason"] == "matched"
    assert opened.screen and "s1│1" in opened.text, opened.text
    # 24 行终端里 less 显示 23 行：用户下移 3 行后才会出现 26。
    terminal.user_bytes(b"jjj")
    scrolled = terminal.read(wait=TerminalWait(until="match", pattern="^26$", timeout_ms=5000))
    assert scrolled.screen and "s1│4" in scrolled.text, scrolled.text
    # 全屏期间行号不被屏幕行占用：进入前的输出仍在原行号上。
    assert terminal.read(start_line=number, limit=1).text.splitlines()[-1] == marker
    terminal.user_bytes(b"q")
    wait_until(lambda: terminal.runtime._observation.buffer.alternate is False)
    assert terminal.read(start_line=number, limit=1).text.splitlines()[-1] == marker


def test_prefill_fills_the_prompt_without_running_it(terminal: _Terminal) -> None:
    """填入只把命令写进行里：屏幕上出现文本，但没有出现命令输出；用户回车后才执行。"""
    if os.name == "nt":
        pytest.skip("prefill output check is POSIX-only")
    wait_until(lambda: terminal.runtime._observation.phase == "editing")
    terminal.prefill("echo PREFILLED")
    time.sleep(1)
    shown = terminal.read(tail=40).text
    assert "echo PREFILLED" in shown, shown
    screen_text = [line.split("│", 1)[-1] for line in shown.splitlines() if "│" in line]
    assert "PREFILLED" not in screen_text, (
        "the filled command must not run before the user presses Enter: " + shown
    )
    terminal.user_bytes(b"\r")
    executed = terminal.read(
        wait=TerminalWait(until="match", pattern="^PREFILLED$", timeout_ms=5000)
    )
    assert executed.wait_result["end_reason"] == "matched", executed.text


def test_match_reports_running_command(terminal: _Terminal) -> None:
    terminal.user("sleep 0.3; echo READY-NOW; sleep 30")
    started = terminal.read(
        wait=TerminalWait(until="match", pattern="READY-NOW$", timeout_ms=10000)
    )
    assert started.wait_result["end_reason"] == "matched", started.text
    # 命令执行中投影 commandRunning，前端据此决定关闭时是否确认。
    assert session_entries(terminal.service, 1)[0]["commandRunning"] is True
    # bash 3.2 在两条简单命令之间（DEBUG trap 内）收到 Ctrl-C 时偶发不报告返回（已知竞态），
    # 等 sleep 真正开始再中断。用户按 Ctrl-C 中断前台命令（Agent 已无 signal 能力，原始字节是用户输入）。
    time.sleep(0.3)
    terminal.user_bytes(b"\x03")
    wait_until(
        lambda: (command := terminal.runtime._observation.latest_command) is not None
        and command.status != "executing",
        timeout=10,
    )
    assert session_entries(terminal.service, 1)[0]["commandRunning"] is False


def test_user_raw_input_and_resize(terminal: _Terminal) -> None:
    service, session_id = terminal.service, terminal.session_id
    assert service.require(session_id, 1).user_input(b"echo typed-by-user\r") == "accepted"
    seen = terminal.read(wait=TerminalWait(until="match", pattern="^typed-by-user$",
                                           timeout_ms=5000))
    assert seen.wait_result["end_reason"] == "matched"
    assert "user typed lines" in seen.text
    service.require(session_id, 1).resize(30, 100)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if geometry(service, session_id) == (30, 100):
            break
        time.sleep(0.05)
    assert geometry(service, session_id) == (30, 100)
    size = terminal.run_user("stty size")
    assert any(line.endswith("│30 100") for line in size.text.splitlines()), size.text


def test_quiet_wait_without_command_return(terminal: _Terminal) -> None:
    terminal.user("cat")
    result = terminal.read(
        wait=TerminalWait(until="output_quiet", quiet_ms=300, timeout_ms=5000)
    )
    assert result.wait_result["end_reason"] == "output_quiet"
    assert "executing `cat`" in result.text
    # 用户按 Ctrl-D（POSIX）或 Ctrl-C（Windows）结束前台程序。
    terminal.user_bytes(b"\x03" if os.name == "nt" else b"\x04")
    wait_until(
        lambda: (command := terminal.runtime._observation.latest_command) is not None
        and command.status != "executing",
        timeout=10,
    )
