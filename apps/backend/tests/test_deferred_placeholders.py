"""后台占位行的结算：Run 外撤销 / 重启、fork 不继承、结论并入时的 Run 内结算与投影规则。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.projection.tool_call_event_projection import plan_tool_calls_settled
from app.core.events import ToolCallsSettledEvent
from app.core.tool.background import placeholders
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.schemas import ToolObservation
from app.core.workflow.node_helper import tool_observation_projection as projection_module
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata
from app.service import depends
from app.service.conversation_run.conversation_task_context_service import _uninherited


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
    crud.create(_tool_row(task.id, run.id, 1, "call-bg", status="pending", deferred=True))
    crud.create(_tool_row(task.id, run.id, 2, "call-hitl", status="pending"))
    crud.create(_tool_row(task.id, run.id, 3, "call-done", status="completed", deferred=True))
    return SimpleNamespace(task_id=task.id, crud=crud)


def _metadata(stored, call_id: str) -> ToolMessageMetadata:
    for record in stored.crud.get(stored.task_id, include_in_context=False):
        if record.message.get("tool_call_id") == call_id:
            return record.metadata
    raise AssertionError(call_id)


def test_only_pending_deferred_rows_are_settled(stored) -> None:
    assert [r.message["tool_call_id"] for r in stored.crud.list_pending_deferred()] == ["call-bg"]

    count = placeholders.settle_placeholders(
        stored.task_id, None, "cancelled", placeholders.INTERRUPTED_HINT
    )

    assert count == 1
    settled = _metadata(stored, "call-bg")
    assert (settled.status, settled.error, settled.deferred) == ("cancelled", "已中断", True)
    # HITL 等待占位与已完成的后台占位不动。
    assert _metadata(stored, "call-hitl").status == "pending"
    assert _metadata(stored, "call-done").status == "completed"
    # 迟到的再次结算幂等。
    assert placeholders.settle_placeholders(stored.task_id, None, "cancelled", None) == 0


def test_settlement_filters_by_call(stored) -> None:
    assert placeholders.settle_placeholders(stored.task_id, ["call-other"], "cancelled", None) == 0
    assert placeholders.settle_placeholders(stored.task_id, [], "cancelled", None) == 0
    assert _metadata(stored, "call-bg").status == "pending"


def test_fork_marks_waiting_background_placeholders_uninherited() -> None:
    waiting = ToolMessageMetadata(status="pending", deferred=True)
    copied = _uninherited(waiting)
    assert (copied.status, copied.error) == ("cancelled", "未继承")
    assert waiting.status == "pending"  # 源 Task 的行不受影响
    hitl = ToolMessageMetadata(status="pending")
    assert _uninherited(hitl).status == "pending"


def test_terminal_event_keeps_deferred_placeholders_pending() -> None:
    observation = ToolObservation(tool_name="terminal_write", status="success", deferred=True)
    event = terminal_event(observation, task_id=1, run_id=2, step_id="s", tool_call_id="c")
    assert (event.status, event.deferred) == ("pending", True)


def test_run_settlement_skips_deferred_parts() -> None:
    state = {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "runId": 3,
                "parts": [
                    {"type": "tool-call", "toolCallId": "a", "status": "pending", "background": True},
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

    def update_tool_result_metadata(self, _call_id: str, metadata: ToolMessageMetadata):
        self.updates.append(metadata)
        return metadata.model_copy(update={"background": True})


@pytest.mark.parametrize("status", ["pending", "cancelled"])
def test_consumed_result_completes_the_placeholder(monkeypatch, status: str) -> None:
    """结论并入即推到 completed；重启改成 cancelled 的占位在结论到达后同样推到 completed。"""
    context = _Context(ToolMessageMetadata(status=status, deferred=True))
    emitted: list[Any] = []
    monkeypatch.setattr(projection_module, "_message_context", lambda: context)
    monkeypatch.setattr(
        projection_module,
        "_runtime_context",
        lambda: SimpleNamespace(run=SimpleNamespace(id=9, task_id=1)),
    )
    monkeypatch.setattr(projection_module, "emit", emitted.append)

    projection_module.settle_deferred_placeholder("call-bg", step_id="step-2")

    assert [update.status for update in context.updates] == ["completed"]
    [event] = emitted
    assert (event.tool_call_id, event.status) == ("call-bg", "completed")


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        ToolMessageMetadata(status="pending"),
        ToolMessageMetadata(status="completed", deferred=True),
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

    projection_module.settle_deferred_placeholder("call-x", step_id="step-2")

    assert context.updates == []
