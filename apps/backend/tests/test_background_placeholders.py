"""后台占位行的结算：Run 外撤销 / 重启、fork 不继承、结论并入时的 Run 内结算与投影规则。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_state_snapshot import _validate_part
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.placeholder_settlement import INTERRUPTED_HINT, cancel_placeholders
from app.core.tool.schemas import ToolObservation
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.message_queue.task_message import BackgroundResultBody
from app.core.workflow.node_helper import tool_observation_projection as projection_module
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
from app.service import depends
from app.tools.tool_handler.terminal.command_job import CommandConclusion
from tests.command_job_fakes import FakeCommandJob

_WORK = BackgroundWork(
    run_id=1, kind="terminal_command", label="make", started_at="2026-01-01T00:00:00+00:00"
)
_ENDED_AT = "2026-01-01T00:01:00+00:00"
_FINISHED_WORK = _WORK.model_copy(update={"ended_at": _ENDED_AT})


def _tool_row(task_id: int, run_id: int, sequence: int, call_id: str, **metadata: Any):
    return ConversationTaskContextRecord(
        task_id=task_id,
        run_id=run_id,
        message={"role": "tool", "tool_call_id": call_id, "content": "placeholder"},
        include_in_context=True,
        sequence=sequence,
        turn=1,
        metadata=ToolMessageMetadata(**metadata),
    )


@pytest.fixture
def stored(tmp_path: Path, isolated_storage):
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    run = depends.get_conversation_run_crud().create(task.id, "completed")
    crud = depends.get_conversation_task_context_crud()
    crud.create(_tool_row(task.id, run.id, 1, "call-bg", status="pending", background=_WORK))
    crud.create(_tool_row(task.id, run.id, 2, "call-hitl", status="pending"))
    crud.create(
        _tool_row(task.id, run.id, 3, "call-done", status="completed", background=_FINISHED_WORK)
    )
    return SimpleNamespace(task_id=task.id, crud=crud)


def _metadata(stored, call_id: str) -> ToolMessageMetadata:
    for record in stored.crud.get(stored.task_id, include_in_context=False):
        if record.message.get("tool_call_id") == call_id:
            return record.metadata
    raise AssertionError(call_id)


def test_only_pending_background_rows_are_settled(stored) -> None:
    assert [r.message["tool_call_id"] for r in stored.crud.list_unsettled_placeholders()] == ["call-bg"]

    settled_tasks = cancel_placeholders(
        stored.task_id, None, INTERRUPTED_HINT
    )

    assert settled_tasks == frozenset({stored.task_id})
    settled = _metadata(stored, "call-bg")
    assert (settled.status, settled.error) == ("cancelled", "已中断")
    assert settled.background.started_at == _WORK.started_at
    assert settled.background.ended_at is not None
    # HITL 等待占位与已完成的后台占位不动。
    assert _metadata(stored, "call-hitl").status == "pending"
    assert _metadata(stored, "call-done").status == "completed"
    assert _metadata(stored, "call-done").background == _FINISHED_WORK
    # 迟到的再次结算幂等。
    assert cancel_placeholders(stored.task_id, None, None) == frozenset()


def test_settlement_filters_by_call(stored) -> None:
    other = cancel_placeholders(stored.task_id, ["call-other"], None)
    empty = cancel_placeholders(stored.task_id, [], None)
    assert other == empty == frozenset()
    assert _metadata(stored, "call-bg").status == "pending"


def test_terminal_event_keeps_background_placeholders_pending() -> None:
    observation = ToolObservation(tool_name="terminal_write", status="success", background=_WORK)
    event = terminal_event(observation, task_id=1, run_id=2, step_id="s", tool_call_id="c")
    assert (event.status, event.background) == ("pending", _WORK)



def test_observe_persists_background_placeholder_as_pending(monkeypatch) -> None:
    """后台占位经 tools → observe 交接后仍带 ``background``，落库为 pending、实时投影也是 pending。"""
    written: list[ToolMessageMetadata] = []
    emitted: list[Any] = []
    monkeypatch.setattr(
        projection_module,
        "_message_context",
        lambda: SimpleNamespace(add_message=lambda _message, *, metadata: written.append(metadata)),
    )
    monkeypatch.setattr(
        projection_module,
        "_runtime_context",
        lambda: SimpleNamespace(run=SimpleNamespace(id=9, task_id=1)),
    )
    monkeypatch.setattr(projection_module, "emit", emitted.append)
    batch = ToolBatch(
        observations=[
            ToolObservation(
                tool_name="execute_terminal",
                status="success",
                tool_call_id="call-bg",
                background=_WORK,
            )
        ],

    )

    projection_module.settle_observations(batch, step_id="step-1")

    [metadata] = written
    assert (metadata.status, metadata.background) == ("pending", _WORK)
    [event] = emitted
    assert (event.status, event.background) == ("pending", _WORK)


def test_background_placeholder_projects_a_valid_part_field() -> None:
    """卡片字段与 snapshot 白名单、前端契约同名（``background``），否则 snapshot 校验会拒收。"""
    fields = project_tool_result(ToolMessageMetadata(status="pending", background=_WORK))

    assert fields.get("background") == {
        "kind": _WORK.kind,
        "label": _WORK.label,
        "startedAt": _WORK.started_at,
        "endedAt": None,
    }
    _validate_part({"presentation": "activity", "type": "tool-call", "toolCallId": "c", "toolName": "t", "args": {}, **fields})


class _Context:
    def __init__(self, metadata: ToolMessageMetadata | None) -> None:
        self.metadata = metadata
        self.updates: list[ToolMessageMetadata] = []

    def tool_result_metadata(self, _call_id: str) -> ToolMessageMetadata | None:
        return self.metadata

    def update_tool_result_metadata(self, _call_id: str, metadata: ToolMessageMetadata, *, session):
        self.updates.append(metadata)
        return metadata


def test_consumed_result_completes_the_placeholder(monkeypatch) -> None:
    """消费结论在认领事务中结算占位；返回提交后待发布的事件。"""
    context = _Context(
        ToolMessageMetadata(
            status="pending", background=_WORK,
        )
    )
    emitted: list[Any] = []
    monkeypatch.setattr(projection_module, "_message_context", lambda: context)
    monkeypatch.setattr(
        projection_module,
        "_runtime_context",
        lambda: SimpleNamespace(run=SimpleNamespace(id=9, task_id=1)),
    )
    monkeypatch.setattr(projection_module, "emit", emitted.append)

    card = {"kind": "terminal-result", "command": "make", "output": "ok", "exit_code": 0}
    body = BackgroundResultBody(tool_call_id="call-bg", tool_name="terminal_command", tool_result="{}",
                                placeholder_display_data=card, ended_at=_ENDED_AT)
    event = projection_module.settle_background_placeholder("call-bg", body, step_id="step-2", session=object())

    # 作业给卡片的终态展示随结算写入（写入即持久化），投影按实际写入的展示数据发出。
    [update] = context.updates
    assert (update.status, update.display_data) == ("completed", card)
    assert update.background == _FINISHED_WORK
    assert not emitted
    assert (event.tool_call_id, event.status, event.display_data) == ("call-bg", "completed", card)
    assert event.background == _FINISHED_WORK


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        ToolMessageMetadata(status="pending"),
        ToolMessageMetadata(status="completed", background=_FINISHED_WORK),
        ToolMessageMetadata(status="cancelled", background=_FINISHED_WORK),
    ],
)
def test_consumed_result_leaves_other_rows_alone(monkeypatch, metadata) -> None:
    context = _Context(metadata)
    monkeypatch.setattr(projection_module, "_message_context", lambda: context)
    monkeypatch.setattr(
        projection_module,
        "_runtime_context",
        lambda: SimpleNamespace(run=SimpleNamespace(id=9, task_id=1)),
    )
    monkeypatch.setattr(projection_module, "emit", lambda _event: pytest.fail("emitted"))

    body = BackgroundResultBody(
        tool_call_id="call-x", tool_name="execute_terminal", tool_result="{}", ended_at=_ENDED_AT
    )
    assert projection_module.settle_background_placeholder("call-x", body, step_id="step-2", session=object()) is None

    assert context.updates == []


def test_result_arriving_while_user_stopping_is_still_delivered(stored) -> None:
    """用户停止只打断当前驱动：停止信号在时后台工作完成，结论照常入队，占位不被撤销。"""
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )
    from app.tools.tool_handler.terminal.command_results import command_results

    command_results.register(
        "call-bg",
        task_id=stored.task_id,
        run_id=None,
        label="terminal:sleep",
        job=FakeCommandJob(),
    )
    cancellation_registry.request_cancel(stored.task_id, "user")
    try:
        assert command_results.complete(
            stored.task_id, "call-bg", CommandConclusion({"exit_code": 0})
        ) is True
    finally:
        cancellation_registry.consume(stored.task_id)
        command_results.drop_task(stored.task_id)

    assert _metadata(stored, "call-bg").status == "pending"
    queued = depends.get_task_message_queue().pending_nowait(stored.task_id)
    assert [message.kind for message in queued] == ["background_result"]


def test_restart_interrupts_hitl_calls_whose_decision_was_executing(stored) -> None:
    """决定已接收、执行被重启打断的 HITL 占位（running）随重启收尾推到「已中断」；仍在等决定的不动。"""
    from app.service.recover_service import RecoverService

    run_id = stored.crud.get(stored.task_id)[0].run_id
    stored.crud.create(_tool_row(stored.task_id, run_id, 4, "call-claimed", status="running"))

    assert RecoverService().interrupt_placeholders([stored.task_id]) == frozenset(
        {stored.task_id}
    )

    claimed = _metadata(stored, "call-claimed")
    assert (claimed.status, claimed.error) == ("cancelled", "已中断")
    assert _metadata(stored, "call-hitl").status == "pending"


def test_cancelled_placeholder_persists_final_display_for_cold_rebuild(stored):
    display = {"kind": "terminal-result", "command": "run", "output": "full tail", "exit_code": -1}
    cancel_placeholders(stored.task_id, ["call-bg"], "已停止", displays={"call-bg": display})
    metadata = _metadata(stored, "call-bg")
    assert metadata.status == "cancelled"
    assert metadata.display_data == display
    assert project_tool_result(metadata)["display_data"] == display


def test_real_parent_stop_persists_output_without_waking_agent(stored, tmp_path):
    import asyncio
    import time

    from app.core.task_recall import TaskRecall
    from app.core.tool.schemas import ToolExecutionContext
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )
    from app.tools.tool_handler.terminal.command_results import command_results
    from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
    from tests.terminal_test_support import terminal_worker_path
    if not terminal_worker_path().is_file():
        pytest.skip("terminal worker not built")
    marker = tmp_path / "started"
    ExecuteTerminalTool().execute(
        command=f"printf '完整取消输出\\n'; touch '{marker}'; sleep 10",
        background=True, execution_context=ToolExecutionContext(
            task_id=stored.task_id, run_id=None, workspace_id=1, workspace_root=tmp_path,
            step_id="test", tool_call_id="call-bg",
        ),
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        cancellation_registry.request_cancel(stored.task_id, "parent")
        recall = TaskRecall(stored.task_id)

        async def recall_and_settle() -> None:
            await recall.settle(await recall.recall())

        asyncio.run(recall_and_settle())
    finally:
        command_results.drop_task(stored.task_id)
        cancellation_registry.consume(stored.task_id)
    # 从 SQLite 重新读取，走实时和冷重建共用的投影器，而不是断言进程内缓存。
    fields = project_tool_result(_metadata(stored, "call-bg"))
    assert fields["status"] == "cancelled"
    assert "完整取消输出" in fields["display_data"]["output"]
    assert depends.get_task_message_queue().pending_nowait(stored.task_id) == []
