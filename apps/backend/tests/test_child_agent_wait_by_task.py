"""child_agent_wait 按 Task 等待：等子任务静止后读其最新 Run，交接出的后续 Run 也在等待范围内。"""

import asyncio
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.child_task import child_agent_wait as wait_module
from app.core.tools.tool_handler.child_task.child_agent_wait import ChildAgentWaitTool

_PARENT_TASK = 9_300_001
_CHILD_TASK = 9_300_002


@pytest.fixture
def main_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """模拟主事件循环：等待在它上面进行，工具在测试线程里阻塞。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


class _Session:
    """调度入口替身：前 ``busy_rounds`` 次等待不静止，期间子任务完成一次交接。"""

    def __init__(self, tasks: dict[int, SimpleNamespace], busy_rounds: int) -> None:
        self._tasks = tasks
        self._busy_rounds = busy_rounds

    async def wait_idle(self, task_id: int, _wait_seconds: float) -> bool:
        if self._busy_rounds == 0:
            return True
        self._busy_rounds -= 1
        # 第一条 Run 正常完成后交接给第二条，Task 的最新 Run 随之改变。
        self._tasks[task_id].current_run_id = 2
        return False


def _tool(tasks, runs) -> ChildAgentWaitTool:
    tool = ChildAgentWaitTool.__new__(ChildAgentWaitTool)
    tool._task_service = SimpleNamespace(get_task=lambda task_id: tasks[task_id])
    tool._run_service = SimpleNamespace(get_run=lambda run_id: runs[run_id])
    return tool


def _context(loop) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=_PARENT_TASK,
        workspace_id=1,
        workspace_root=Path("."),
        step_id="step-1",
        run_id=1,
        event_loop=loop,
    )


def test_wait_returns_latest_run_output_after_task_becomes_idle(monkeypatch, main_loop):
    tasks = {
        _CHILD_TASK: SimpleNamespace(is_child=True, parent_task_id=_PARENT_TASK, current_run_id=1)
    }
    runs = {
        1: SimpleNamespace(status="completed", final_output="first", end_reason="stop"),
        2: SimpleNamespace(status="completed", final_output="handed off", end_reason="stop"),
    }
    monkeypatch.setattr(wait_module, "get_workflow_session", lambda: _Session(tasks, 1))

    observation = _tool(tasks, runs).execute(
        _CHILD_TASK, timeout_seconds=5, execution_context=_context(main_loop)
    )

    assert observation.status == "success"
    assert json.loads(observation.content)["final_output"] == "handed off"
    assert "child_run_id" not in json.dumps(observation.display_data)


def test_wait_times_out_while_child_task_is_not_idle(monkeypatch, main_loop):
    tasks = {
        _CHILD_TASK: SimpleNamespace(is_child=True, parent_task_id=_PARENT_TASK, current_run_id=1)
    }
    runs = {1: SimpleNamespace(status="waiting_for_input", final_output=None, end_reason=None)}

    class _Waiting:
        async def wait_idle(self, _task_id: int, _wait_seconds: float) -> bool:
            return False

    monkeypatch.setattr(wait_module, "get_workflow_session", lambda: _Waiting())

    observation = _tool(tasks, runs).execute(
        _CHILD_TASK, timeout_seconds=0.01, execution_context=_context(main_loop)
    )

    payload = json.loads(observation.content)
    assert (payload["timed_out"], payload["status"]) == (True, "waiting_for_input")
