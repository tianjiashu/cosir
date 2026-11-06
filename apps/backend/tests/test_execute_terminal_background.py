"""execute_terminal 的前后台执行：前台结论、后台占位、用户转到后台、停止命令。

命令走真实的 ``LocalExecutionBackend``（经 Terminal Worker 跑 POSIX ``sleep`` / ``echo``），后台回投替换为记录调用，
不写 SQLite。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.background import backgraound_registry as background_module
from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler import execute_terminal as execute_terminal_module
from app.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.terminal import LocalExecutionBackend
from tests.terminal_test_support import requires_terminal_worker

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep/echo"),
    requires_terminal_worker,
]

_TASK_ID = 31


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str, dict[str, Any]]]:
    calls: list[tuple[str | None, str, dict[str, Any]]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None) -> bool:
        calls.append((tool_call_id, tool_name, {**result, "card": placeholder_display_data}))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    yield calls
    background_registry.drop_task(_TASK_ID)


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
        description="test", command="echo hello", execution_context=_context(tmp_path, "call-fg")
    )
    assert observation.status == "success" and not observation.background
    assert observation.content.startswith("[exit_code=0]") and "hello" in observation.content
    assert not background_registry.owns(_TASK_ID, "call-fg")
    assert delivered == []


def test_background_command_returns_a_placeholder_then_delivers(tmp_path, delivered) -> None:
    observation = ExecuteTerminalTool().execute(
        description="test", command="sleep 0.2; echo done",
        background=True,
        execution_context=_context(tmp_path, "call-bg"),
    )
    # Agent 主动选后台：先做别的，不挂起。
    assert observation.background and not observation.suspend
    assert (observation.background.kind, observation.background.label) == (
        "terminal_command", "test",
    )
    assert observation.background.ended_at is None
    assert observation.display_data == {
        "kind": "terminal-result",
        "command": "sleep 0.2; echo done",
        "workdir": str(tmp_path.resolve()),
    }
    _wait_for(lambda: len(delivered) == 1)
    tool_call_id, tool_name, payload = delivered[0]
    assert (tool_call_id, tool_name) == ("call-bg", "terminal_command")
    assert payload["end_reason"] == "exited" and payload["exit_code"] == 0
    assert "done" in str(payload["output"])
    # 占位卡片的终态展示随结论走：完整输出与退出码，不经模型的输出预算。
    card = payload["card"]
    assert card["kind"] == "terminal-result" and card["exit_code"] == 0
    assert card["command"] == "sleep 0.2; echo done" and "done" in card["output"]


def test_detached_foreground_command_keeps_running_without_suspending(tmp_path, delivered) -> None:
    """前台没有时限：只有用户转走才离开前台，转走后照样回投结论、不挂起 Agent。"""
    import threading

    box: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: box.setdefault(
            "observation",
            ExecuteTerminalTool().execute(
                description="test",
                command="sleep 0.5; echo late",
                execution_context=_context(tmp_path, "call-detach"),
            ),
        )
    )
    thread.start()
    _wait_for(lambda: background_registry.owns(_TASK_ID, "call-detach"))
    assert background_registry.detach("call-detach") == "detached"
    thread.join(2)
    observation = box["observation"]
    assert observation.background and not observation.suspend
    assert "<background_result>" in observation.content
    _wait_for(lambda: len(delivered) == 1)
    assert "late" in str(delivered[0][2]["output"])


def test_cancelling_a_background_command_kills_its_process_tree(tmp_path, delivered) -> None:
    marker = tmp_path / "finished"
    ExecuteTerminalTool().execute(
        description="test",
        command=f"sleep 2; touch {marker}",
        background=True,
        execution_context=_context(tmp_path, "call-cancel"),
    )
    dropped = background_registry.withdraw("call-cancel")
    assert dropped
    time.sleep(2.5)
    # 进程树被杀：命令没有跑完；撤销后的结论被登记表丢弃，不回投。
    assert not marker.exists()
    assert delivered == []


def test_background_payload_goes_through_the_output_budget(
    tmp_path, delivered, monkeypatch
) -> None:
    from app.core.tool.execute.tool_output_budget import ToolOutputBudget

    monkeypatch.setattr(execute_terminal_module, "ToolOutputBudget", lambda: ToolOutputBudget(200))
    ExecuteTerminalTool().execute(
        description="test",
        command="seq 1 2000",
        background=True,
        execution_context=_context(tmp_path, "call-big"),
    )
    _wait_for(lambda: len(delivered) == 1)
    output = str(delivered[0][2]["output"])
    assert len(output) <= 200 and "2000" in output


def test_local_backend_kills_the_command_when_stopped(tmp_path) -> None:
    marker = tmp_path / "finished"
    backend = LocalExecutionBackend()
    threading.Timer(0.2, backend.stop).start()
    started = time.monotonic()
    result = backend.execute(f"sleep 2; touch {marker}", str(tmp_path), 30)
    assert result.cancelled and not result.timed_out and result.exit_code == -1
    assert time.monotonic() - started < 1.5
    time.sleep(2)
    assert not marker.exists()


def test_parent_stop_retains_real_output_without_delivery(tmp_path, delivered) -> None:
    """停止先结束进程，最终输出写回原卡；不把取消结论回投模型。"""
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )
    marker = tmp_path / "ready"
    ExecuteTerminalTool().execute(
        description="partial output", command=f"printf '已收集\\n'; touch '{marker}'; sleep 10",
        background=True, execution_context=_context(tmp_path, "call-parent-stop"),
    )
    _wait_for(marker.exists)
    cancellation_registry.request_cancel(_TASK_ID, "user")
    try:
        displays = background_registry.drop_task(_TASK_ID)
    finally:
        cancellation_registry.consume(_TASK_ID)
    assert "已收集" in displays["call-parent-stop"]["output"]
    assert delivered == []


def test_timeout_retains_real_output(tmp_path) -> None:
    result = ExecuteTerminalTool().execute(
        description="timeout", command="printf 'before-timeout\\n'; sleep 10", timeout=0.2,
        execution_context=_context(tmp_path, "call-timeout-output"),
    )
    assert result.status == "error"
    assert result.display_data["timed_out"] is True
    assert "before-timeout" in result.display_data["output"]


def test_foreground_cancel_retains_real_output(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    marker = tmp_path / "ready"
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            ExecuteTerminalTool().execute,
            description="cancel", command=f"printf 'before-cancel\\n'; touch '{marker}'; sleep 10",
            execution_context=_context(tmp_path, "call-cancel-output"),
        )
        _wait_for(marker.exists)
        assert background_registry.request_user_cancel(
            _TASK_ID, "call-cancel-output", driving=False
        )
        observation = result.result(timeout=5)
    assert observation.status == "cancelled"
    assert "before-cancel" in observation.display_data["output"]
