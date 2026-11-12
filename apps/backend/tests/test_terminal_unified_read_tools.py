"""统一终端读取的真跑集成：后台命令可被 terminal_list / terminal_read / terminal_stop 访问。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.tools.terminal_runtime.command.running_command_registry import running_commands
from app.tools.tool_handler.terminal.command_results import CommandResultRegistry, command_results
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.terminal.terminal_list import TerminalListTool
from app.tools.tool_handler.terminal.terminal_read import TerminalReadTool
from app.tools.tool_handler.terminal.terminal_stop import TerminalStopTool
from tests.terminal_test_support import requires_terminal_worker

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep/printf"),
    requires_terminal_worker,
]

_TASK_ID = 41


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str, dict[str, Any]]]:
    calls: list[tuple[str | None, str, dict[str, Any]]] = []

    def deliver(task_id, tool_call_id, conclusion, *, starts_run=True) -> bool:
        calls.append(
            (tool_call_id, "execute_terminal", {**conclusion.result, "card": conclusion.display_data})
        )
        return True

    def report_cancelled(task_id, tool_call_id, conclusion) -> bool:
        calls.append((tool_call_id, "execute_terminal", {
            **conclusion.result, "cancel_hint": conclusion.cancel_hint,
            "card": conclusion.display_data,
        }))
        return True

    monkeypatch.setattr(CommandResultRegistry, "_enqueue_result", staticmethod(deliver))
    monkeypatch.setattr(
        CommandResultRegistry, "_enqueue_cancel_receipt", staticmethod(report_cancelled),
    )
    yield calls
    # 收尾：撤销这个 Task 全部后台条目，并等作业线程把登记表条目清掉。
    command_results.drop_task(_TASK_ID)
    deadline = time.monotonic() + 10
    while running_commands.task_commands(_TASK_ID) and time.monotonic() < deadline:
        time.sleep(0.02)


def _context(tmp_path: Path, tool_call_id: str, task_id: int = _TASK_ID) -> ToolExecutionContext:
    return ToolExecutionContext(
        call_intent="test",
        step_id="step-1",
        task_id=task_id,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=1,
        tool_call_id=tool_call_id,
    )


def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_background_command_can_be_read_listed_and_stopped(tmp_path, delivered) -> None:
    tool = ExecuteTerminalTool()
    placeholder = tool.execute(
        command="printf 'booting\\nready\\n'; sleep 30",
        background=True,
        execution_context=_context(tmp_path, "call-bg"),
    )
    # 占位正文带 terminal_id，Agent 不用猜怎么读它。
    assert "terminal_id: call-bg" in (placeholder.content or "")

    read = TerminalReadTool().execute(
        terminal_id="call-bg",
        wait={"until": "match", "pattern": "ready", "quiet_ms": None, "timeout_ms": 10000},
        execution_context=_context(tmp_path, "call-read"),
    )
    assert read.status == "success", read.content
    assert "ready" in (read.content or "")
    assert (read.content or "").startswith("command call-bg · sh · running")

    listed = TerminalListTool().execute(execution_context=_context(tmp_path, "call-list"))
    assert "command call-bg · sh · running" in (listed.content or "")

    stopped = TerminalStopTool().execute(
        terminal_id="call-bg", execution_context=_context(tmp_path, "call-stop")
    )
    assert stopped.status == "success", stopped.content
    _wait_for(lambda: len(delivered) == 1)
    assert delivered[0][2]["end_reason"] == "cancelled"

    # 结束即从登记表移除：再读只剩「这个 Task 里没有这个终端」。
    after = TerminalReadTool().execute(
        terminal_id="call-bg", execution_context=_context(tmp_path, "call-after")
    )
    assert after.status == "error"
    assert "no terminal call-bg" in (after.content or "")


def test_stop_and_read_reject_unknown_or_foreign_terminals(tmp_path) -> None:
    # 不存在的 id：读、停都返回受控错误。
    missing_read = TerminalReadTool().execute(
        terminal_id="call-missing", execution_context=_context(tmp_path, "call-r1")
    )
    assert missing_read.status == "error" and "no terminal call-missing" in (
        missing_read.content or ""
    )
    missing_stop = TerminalStopTool().execute(
        terminal_id="call-missing", execution_context=_context(tmp_path, "call-s1")
    )
    assert missing_stop.status == "error" and "no terminal call-missing" in (
        missing_stop.content or ""
    )

    # 另一个 Task 看不到这个 Task 的命令。
    ExecuteTerminalTool().execute(
        command="sleep 30",
        background=True,
        execution_context=_context(tmp_path, "call-owned"),
    )
    foreign = TerminalReadTool().execute(
        terminal_id="call-owned",
        execution_context=_context(tmp_path, "call-r2", task_id=_TASK_ID + 1),
    )
    assert foreign.status == "error" and "no terminal call-owned" in (foreign.content or "")


def test_foreground_output_is_normalized_for_the_model_and_raw_on_the_card(tmp_path) -> None:
    observation = ExecuteTerminalTool().execute(
        command="printf '\\033[31mred\\033[0m\\n'",
        execution_context=_context(tmp_path, "call-color"),
    )

    assert observation.status == "success"
    assert "red" in (observation.content or "")
    assert "\x1b" not in (observation.content or "")
    assert observation.display_data is not None
    assert "\x1b[31m" in str(observation.display_data["output"])


def test_read_wakes_when_the_command_ends_by_itself(tmp_path, delivered) -> None:
    ExecuteTerminalTool().execute(
        command="sleep 1; printf 'x\\n'; exit 3",
        background=True,
        execution_context=_context(tmp_path, "call-end"),
    )
    started = time.monotonic()
    result = TerminalReadTool().execute(
        terminal_id="call-end",
        wait={"until": "match", "pattern": "never", "quiet_ms": None, "timeout_ms": 10000},
        execution_context=_context(tmp_path, "call-wait"),
    )
    assert time.monotonic() - started < 5
    assert result.status == "success"
    assert "exited" in (result.content or "") and "exit 3" in (result.content or "")
    assert (result.display_data or {})["wait_result"]["end_reason"] == "command_ended"
    _wait_for(lambda: len(delivered) == 1)
    assert delivered[0][2]["exit_code"] == 3


def test_a_shell_gone_after_startup_is_a_start_failure(tmp_path, delivered, monkeypatch) -> None:
    """shell 在工具启动后才消失：执行后端报启动失败，输出照常由调用方封存，不变成内部错误。"""
    from app.tools.terminal_runtime.command import local_backend

    tool = ExecuteTerminalTool()
    monkeypatch.setattr(local_backend, "find_shell", lambda _name: None)

    result = tool.execute(  # noqa: S604 - 显式 shell 取值，find_shell 已被替换，不会启动
        command="echo hi", shell="sh", execution_context=_context(tmp_path, "call-gone")
    )

    assert result.status == "error"
    assert "[start_failed]" in (result.content or "")
    assert "sh is not available" in (result.content or "")
    assert running_commands.task_commands(_TASK_ID) == []
