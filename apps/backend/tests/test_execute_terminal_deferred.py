"""execute_terminal 的前后台执行：前台结论、后台占位、到上限自动转后台、取消杀进程树。

命令走真实的 ``LocalExecutionBackend``（POSIX ``sleep`` / ``echo``），后台回投替换为记录调用，
不写 SQLite。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.background import deferred_registry as deferred_module
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.tool.schemas import DeferredCapability, ToolExecutionContext
from app.tools.tool_handler import execute_terminal as execute_terminal_module
from app.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.terminal import LocalExecutionBackend

pytestmark = pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep/echo")

_TASK_ID = 31


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str, dict[str, Any]]]:
    calls: list[tuple[str | None, str, dict[str, Any]]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result) -> bool:
        calls.append((tool_call_id, tool_name, result))
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", deliver)
    monkeypatch.setattr(execute_terminal_module, "read_terminal_deny_patterns", lambda: ())
    yield calls
    deferred_registry.drop_task(_TASK_ID)


def _context(tmp_path: Path, tool_call_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=_TASK_ID,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=1,
        tool_call_id=tool_call_id,
    )


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_foreground_command_returns_its_result_without_registering(tmp_path, delivered) -> None:
    observation = ExecuteTerminalTool().execute(
        command="echo hello", execution_context=_context(tmp_path, "call-fg")
    )
    assert observation.status == "success" and not observation.deferred
    assert observation.content.startswith("[exit_code=0]") and "hello" in observation.content
    assert not deferred_registry.owns(_TASK_ID, "call-fg")
    assert delivered == []


def test_background_command_returns_a_placeholder_then_delivers(tmp_path, delivered) -> None:
    observation = ExecuteTerminalTool().execute(
        command="sleep 0.2; echo done",
        background=True,
        execution_context=_context(tmp_path, "call-bg"),
    )
    # Agent 主动选后台：先做别的，不挂起。
    assert observation.deferred and not observation.suspend
    assert observation.display_data == {
        "kind": "terminal-result",
        "command": "sleep 0.2; echo done",
        "workdir": str(tmp_path.resolve()),
        "background": True,
    }
    _wait_for(lambda: len(delivered) == 1)
    tool_call_id, tool_name, payload = delivered[0]
    assert (tool_call_id, tool_name) == ("call-bg", "terminal_command")
    assert payload["end_reason"] == "exited" and payload["exit_code"] == 0
    assert "done" in str(payload["output"])


def test_foreground_limit_moves_the_command_to_background_and_suspends(
    tmp_path, delivered, monkeypatch
) -> None:
    monkeypatch.setattr(
        execute_terminal_module,
        "EXECUTE_TERMINAL_DEFERRED",
        DeferredCapability(default_background=False, foreground_limit_s=0.1),
    )
    observation = ExecuteTerminalTool().execute(
        command="sleep 0.5; echo late", execution_context=_context(tmp_path, "call-limit")
    )
    assert observation.deferred and observation.suspend
    assert "<background_result>" in observation.content
    _wait_for(lambda: len(delivered) == 1)
    assert "late" in str(delivered[0][2]["output"])


def test_cancelling_a_background_command_kills_its_process_tree(tmp_path, delivered) -> None:
    marker = tmp_path / "finished"
    ExecuteTerminalTool().execute(
        command=f"sleep 2; touch {marker}",
        background=True,
        execution_context=_context(tmp_path, "call-cancel"),
    )
    dropped = deferred_registry.cancel("call-cancel")
    assert dropped is not None and dropped.mode == "background"
    time.sleep(2.5)
    # 进程树被杀：命令没有跑完；撤销后的结论被登记表丢弃，不回投。
    assert not marker.exists()
    assert delivered == []


def test_background_payload_goes_through_the_output_budget(tmp_path, delivered, monkeypatch) -> None:
    from app.core.tool.execute.tool_output_budget import ToolOutputBudget

    monkeypatch.setattr(execute_terminal_module, "ToolOutputBudget", lambda: ToolOutputBudget(200))
    ExecuteTerminalTool().execute(
        command="seq 1 2000",
        background=True,
        execution_context=_context(tmp_path, "call-big"),
    )
    _wait_for(lambda: len(delivered) == 1)
    output = str(delivered[0][2]["output"])
    assert len(output) <= 200 and "2000" in output


def test_local_backend_kills_the_command_when_cancelled(tmp_path) -> None:
    marker = tmp_path / "finished"
    started = time.monotonic()
    result = LocalExecutionBackend().execute(
        f"sleep 2; touch {marker}",
        str(tmp_path),
        30,
        is_cancelled=lambda: time.monotonic() - started > 0.2,
    )
    assert result.cancelled and not result.timed_out and result.exit_code == -1
    assert time.monotonic() - started < 1.5
    time.sleep(2)
    assert not marker.exists()

