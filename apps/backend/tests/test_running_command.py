"""``RunningCommand`` / ``RunningCommandRegistry``：运行中的命令是一条可读、可等、可停的终端。"""

from __future__ import annotations

import time

import pytest
from terminal_test_support import run_in_thread

from app.config.constant import Constant
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.command.running_command import RunningCommand
from app.tools.terminal_runtime.command.running_command_registry import RunningCommandRegistry
from app.tools.terminal_runtime.observation.wait import TerminalWait


def never() -> bool:
    return False


def _command(command: str = "npm run dev") -> RunningCommand:
    return RunningCommand(
        command_id="call-1", command=command, shell_kind="sh", cwd="/workspace"
    )


def _read(command: RunningCommand, **kwargs: object):
    return command.read(is_cancelled=never, **kwargs)  # type: ignore[arg-type]


def test_read_renders_a_command_terminal_with_numbered_lines() -> None:
    command = _command()
    command.feed(["booting", "ready"], "")

    result = _read(command)

    assert result.text.startswith(
        "command call-1 · sh · running · executing `npm run dev` (cmd 1) · cwd /workspace"
    )
    assert "1│booting" in result.text
    assert "2│ready" in result.text
    assert result.terminal_id == "call-1"


def test_pending_line_is_visible_and_matchable() -> None:
    command = _command()
    command.feed([], "Listening on :3000")

    result = _read(command, wait=TerminalWait(until="match", pattern="Listening on", timeout_ms=2000))

    assert result.wait_result == {"until": "match", "end_reason": "matched", "matched_line": 1}
    assert "Listening on :3000" in result.text


def test_wait_conditions_are_woken_by_feed_and_finish() -> None:
    command = _command()
    thread, box = run_in_thread(
        lambda: _read(
            command,
            wait=TerminalWait(until="match", pattern="^ready$", timeout_ms=5000),
        )
    )
    time.sleep(0.05)
    command.feed(["ready"], "")
    thread.join(2)
    assert box["result"].wait_result["end_reason"] == "matched"

    quiet = _read(
        command, wait=TerminalWait(until="output_quiet", quiet_ms=100, timeout_ms=2000)
    )
    assert quiet.wait_result["end_reason"] == "output_quiet"

    returned_thread, returned = run_in_thread(
        lambda: _read(command, wait=TerminalWait(until="command_returned", timeout_ms=5000))
    )
    time.sleep(0.05)
    command.finish(ExecutionResult(end="exited", exit_code=0))
    returned_thread.join(2)
    assert returned["result"].wait_result["end_reason"] == "command_returned"


def test_wait_settles_by_its_condition_or_by_the_command_ending() -> None:
    """来源结束时的统一规则：已成立的条件优先，否则 command_ended（不再借用 session_ended）。"""
    unmet = _command()
    thread, box = run_in_thread(
        lambda: _read(unmet, wait=TerminalWait(until="match", pattern="never", timeout_ms=5000))
    )
    time.sleep(0.05)
    unmet.finish(ExecutionResult(end="exited", exit_code=3))
    thread.join(2)
    assert box["result"].wait_result["end_reason"] == "command_ended"

    met = _command()
    thread, box = run_in_thread(
        lambda: _read(met, wait=TerminalWait(until="match", pattern="^ready$", timeout_ms=5000))
    )
    time.sleep(0.05)
    # 最后一批输出与结束在同一次唤醒前到达：等待按已成立的条件结算。
    with met._condition:
        met.feed(["ready"], "")
        met.finish(ExecutionResult(end="exited", exit_code=0))
    thread.join(2)
    assert box["result"].wait_result["end_reason"] == "matched"


def test_finished_command_reports_its_end_in_the_header() -> None:
    command = _command()
    command.feed([], "done")
    command.finish(ExecutionResult(end="exited", exit_code=3))

    text = _read(command).text
    assert "ended" in text and "exited" in text and "exit 3" in text

    cancelled = _command("sleep 9")
    cancelled.finish(ExecutionResult(end="cancelled"))
    assert "cancelled" in _read(cancelled).text


def test_registry_holds_commands_until_they_finish() -> None:
    registry = RunningCommandRegistry()
    first = registry.start(
        task_id=1, command_id="call-1", command="a", shell_kind="sh", cwd="/w"
    )
    registry.start(task_id=1, command_id="call-2", command="b", shell_kind="sh", cwd="/w")
    registry.start(task_id=2, command_id="call-1", command="c", shell_kind="sh", cwd="/w")
    assert registry.get(1, "call-1") is first
    assert [command.terminal_id for command in registry.task_commands(1)] == ["call-1", "call-2"]
    assert [command.terminal_id for command in registry.task_commands(2)] == ["call-1"]

    with pytest.raises(RuntimeError):
        registry.start(task_id=1, command_id="call-1", command="x", shell_kind="sh", cwd="/w")

    registry.finish(1, "call-1", ExecutionResult(end="exited", exit_code=0))
    with pytest.raises(KeyError):
        registry.get(1, "call-1")
    # 重复 finish 不产生副作用。
    registry.finish(1, "call-1", ExecutionResult(end="exited", exit_code=0))


def test_line_buffer_eviction_keeps_line_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Constant.Terminal, "MAX_BUFFER_LINES", 5)
    command = _command()
    command.feed([f"line-{index}" for index in range(10)], "")

    result = _read(command)
    assert "1│" not in result.text
    assert "6│line-5" in result.text
    assert "10│line-9" in result.text


def test_polling_a_quiet_command_whose_output_ends_with_a_newline_says_no_change() -> None:
    """管道输出以换行结尾时可变区为空：轮询末尾与搜索仍按「无变化」提示改用 wait，回看历史不提示。"""
    command = _command()
    command.feed(["booting", "listening on 3000"], "")
    _read(command)
    assert "no change since your last read" in _read(command).text
    _read(command, pattern="listening")
    assert "no change since your last read" in _read(command, pattern="listening").text
    _read(command, start_line=1, limit=1)
    assert "no change" not in _read(command, start_line=1, limit=1).text
