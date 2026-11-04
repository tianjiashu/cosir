"""后台占位行的结算：Run 外撤销 / 重启、fork 不继承、结论并入时的 Run 内结算与投影规则。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.projection.tool_call_event_projection import plan_tool_calls_settled
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_state_snapshot import _validate_part
from app.core.events import ToolCallsSettledEvent
from app.core.tool.background import placeholders
from app.core.tool.background.job import BackgroundConclusion
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.schemas import ToolObservation
from app.core.workflow.execution_state.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.message_queue.task_message import BackgroundResultBody
from app.core.workflow.node_helper import tool_observation_projection as projection_module
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
from app.service import depends
from app.service.conversation_run.conversation_task_context_service import _uninherited

_WORK = BackgroundWork(
    kind="terminal_command", label="make", started_at="2026-01-01T00:00:00+00:00"
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

    count = placeholders.settle_placeholders(
        stored.task_id, None, "cancelled", placeholders.INTERRUPTED_HINT
    )

    assert count == 1
    settled = _metadata(stored, "call-bg")
    assert (settled.status, settled.error) == ("cancelled", "已中断")
    assert settled.background.started_at == _WORK.started_at
    assert settled.background.ended_at is not None
    # HITL 等待占位与已完成的后台占位不动。
    assert _metadata(stored, "call-hitl").status == "pending"
    assert _metadata(stored, "call-done").status == "completed"
    assert _metadata(stored, "call-done").background == _FINISHED_WORK
    # 迟到的再次结算幂等。
    assert placeholders.settle_placeholders(stored.task_id, None, "cancelled", None) == 0


def test_settlement_filters_by_call(stored) -> None:
    assert placeholders.settle_placeholders(stored.task_id, ["call-other"], "cancelled", None) == 0
    assert placeholders.settle_placeholders(stored.task_id, [], "cancelled", None) == 0
    assert _metadata(stored, "call-bg").status == "pending"


def test_fork_marks_waiting_background_placeholders_uninherited() -> None:
    waiting = ToolMessageMetadata(status="pending", background=_WORK)
    copied = _uninherited(waiting)
    assert (copied.status, copied.error) == ("cancelled", "未继承")
    assert waiting.status == "pending"  # 源 Task 的行不受影响
    assert waiting.background.ended_at is None
    assert copied.background.started_at == _WORK.started_at
    assert copied.background.ended_at is not None
    finished = ToolMessageMetadata(status="completed", background=_FINISHED_WORK)
    assert _uninherited(finished).background == _FINISHED_WORK
    hitl = ToolMessageMetadata(status="pending")
    assert _uninherited(hitl).status == "pending"


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
        valid_tool_calls={
            "call-bg": ValidToolCall(name="execute_terminal", projection="projected")
        },
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
    _validate_part({"type": "tool-call", "toolCallId": "c", "toolName": "t", "args": {}, **fields})

def test_run_settlement_skips_background_parts() -> None:
    state = {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "runId": 3,
                "parts": [
                    {
                        "type": "tool-call",
                        "toolCallId": "a",
                        "status": "pending",
                        "background": {
                            "kind": "terminal_command",
                            "label": "build",
                            "startedAt": "2026-01-01T00:00:00+00:00",
                            "endedAt": None,
                        },
                    },
                    {"type": "tool-call", "toolCallId": "b", "status": "running"},
                ],
            }
        ]
    }
    event = ToolCallsSettledEvent(task_id=1, run_id=3, status="cancelled", reason="run_cancelled")
    mutations = plan_tool_calls_settled(event, state)  # type: ignore[arg-type]
    touched = {mutation.path[:-1] for mutation in mutations}
    assert touched == {("messages", 0, "parts", 1)}


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

    body = BackgroundResultBody(tool_name="terminal_command", tool_result="{}", ended_at=_ENDED_AT)
    assert projection_module.settle_background_placeholder("call-x", body, step_id="step-2", session=object()) is None

    assert context.updates == []


def test_result_arriving_while_stopping_settles_placeholder_as_cancelled(stored) -> None:
    """停止信号已打出、收尾撤销之前后台工作恰好完成：占位仍随撤销推到 cancelled，不悬空在 pending。"""
    import asyncio

    from app.core.task_cancellation import TaskCancellation
    from app.core.tool.background.backgraound_registry import background_registry
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )

    background_registry.register(
        "call-bg",
        task_id=stored.task_id,
        run_id=None,
        kind="terminal_command",
        label="terminal:sleep",
        on_cancel=lambda: None,
    )
    cancellation_registry.request_cancel(stored.task_id, "user")
    try:
        assert background_registry.complete("call-bg", BackgroundConclusion({"exit_code": 0})) is False
        asyncio.run(TaskCancellation(stored.task_id).cancel_background_task())
    finally:
        cancellation_registry.consume(stored.task_id)
        background_registry.drop_task(stored.task_id)

    assert _metadata(stored, "call-bg").status == "cancelled"
    assert depends.get_task_message_queue().pending_nowait(stored.task_id) == []


def test_restart_interrupts_hitl_calls_whose_decision_was_executing(stored) -> None:
    """决定已接收、执行被重启打断的 HITL 占位（running）随重启收尾推到「已中断」；仍在等决定的不动。"""
    from app.core.tool.background.restart_settlement import interrupt_pending_placeholders

    run_id = stored.crud.get(stored.task_id)[0].run_id
    stored.crud.create(_tool_row(stored.task_id, run_id, 4, "call-claimed", status="running"))

    interrupt_pending_placeholders()

    claimed = _metadata(stored, "call-claimed")
    assert (claimed.status, claimed.error) == ("cancelled", "已中断")
    assert _metadata(stored, "call-hitl").status == "pending"
