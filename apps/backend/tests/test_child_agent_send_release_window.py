"""子 Run 已落定终态、执行器仍在收尾时追加输入，子 Task 上不得出现两个并发执行。

真实 SQLite + 真实调度入口，只替换单次 Run 的驱动：首个 Run 落定 completed 后继续
占住执行 0.3s，模拟释放边界（交接、收尾）尚未结束的窗口；``child_agent_send`` 在这个窗口里
从工具线程发起。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.child_task.child_agent_send import ChildAgentSendTool
from app.models import ConversationRunCommand
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.service.conversation_run import workflow_session as session_module
from app.service.depends import get_conversation_run_state_service, get_workflow_session
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.task_message_crud import TaskMessageCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.model.model_config_model import ModelConfigModel
from app.storage.store_engines import main_session_factory

_RELEASE_WINDOW_SECONDS = 0.3


class _Runtime:
    """确认本次执行投递的输入并立即完成；首个 Run 完成后继续占住执行，制造收尾窗口。"""

    def __init__(self) -> None:
        self.started: list[int] = []

    async def drive(self, run_id: int, mailbox, initial_delivery) -> None:
        await initial_delivery()
        # 只确认本次执行收到的投递，与 prepare_input 一致。
        with main_session_factory().begin() as session:
            TaskMessageCrud().delete_returning(
                session, [m.id for m in mailbox.drain() if m.id is not None]
            )
        get_conversation_run_state_service().transition(
            run_id, RunTransition.COMPLETE, final_output="done"
        )
        first = not self.started
        self.started.append(run_id)
        if first:
            await asyncio.sleep(_RELEASE_WINDOW_SECONDS)


@pytest.fixture
def child_task(isolated_storage, tmp_path, monkeypatch):
    workspace = WorkspaceCrud().create("child", str(tmp_path))
    parent = TaskCrud().create(workspace.id, "parent", tool_schemas=[])
    child = TaskCrud().create(
        workspace.id, "child", task_type="delegate_task", parent_task_id=parent.id, tool_schemas=[]
    )
    with main_session_factory().begin() as session:
        config = ModelConfigModel(
            config_name="test",
            base_url="https://example.invalid",
            api_key="test",
            model_name="test",
            context_window_k=32,
        )
        session.add(config)
        session.flush()
        config_id = config.id
    runtime = _Runtime()
    monkeypatch.setattr(session_module, "drive_run", runtime.drive)
    child_profile = SimpleNamespace(
        agent_type=AgentProfileType.CHILD, model_settings=None, model_config_id=None
    )
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: SimpleNamespace(resolve=lambda _root, _agent_id: child_profile),
    )
    return SimpleNamespace(
        workspace=workspace, parent=parent, child=child, config_id=config_id, runtime=runtime
    )


async def test_send_during_release_window_never_runs_two_executions(child_task):
    session = get_workflow_session()
    task_id = child_task.child.id

    def executions() -> int:
        return sum(1 for item in session._executions.values() if item.task_id == task_id)

    peak = 0
    stop = asyncio.Event()

    async def watch() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, executions())
            await asyncio.sleep(0.005)

    watcher = asyncio.create_task(watch())
    await session.submit(
        task_id,
        ConversationRunCommand(display_text="first"),
        model_config_id=child_task.config_id,
        reasoning_effort=None,
        delivery="immediate",
    )
    first_run = TaskCrud().get(task_id).current_run_id
    for _ in range(100):
        if ConversationRunCrud().get(first_run).status == ConversationRunStatus.COMPLETED.value:
            break
        await asyncio.sleep(0.01)
    # 首个 Run 已完成但执行仍登记：这正是 child_agent_wait 返回、父 Agent 追加输入的窗口。
    assert executions() == 1

    context = ToolExecutionContext(
        task_id=child_task.parent.id,
        workspace_id=child_task.workspace.id,
        workspace_root=Path(child_task.workspace.root_path),
        step_id="step-1",
        run_id=0,
        event_loop=asyncio.get_running_loop(),
    )
    observation = await asyncio.to_thread(
        ChildAgentSendTool().execute,
        child_task_id=task_id,
        message="again",
        execution_context=context,
    )
    assert observation.status == "success", observation

    for _ in range(500):
        if executions() == 0:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await watcher

    runs = ConversationRunCrud().list_by_task(task_id)
    assert peak == 1
    assert [run.status for run in runs] == [ConversationRunStatus.COMPLETED.value] * 2
    assert len(child_task.runtime.started) == 2
