"""后台看板是工作副本里工具 part 与登记表活条目的纯函数。

覆盖收尾中、活条目先于占位写入、截取与冷热一致。
"""

from dataclasses import replace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_background_task import (
    project_background_tasks,
    validate_background_tasks,
)
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.placeholder_settlement import cancel_placeholders
from app.tools.tool_handler.terminal.command_results import command_results
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
from app.service import depends

pytestmark = pytest.mark.usefixtures("empty_tool_registry")

_START = "2026-01-01T00:00:00+00:00"


def _work(label: str, *, started_at: str = _START, ended_at: str | None = None) -> BackgroundWork:
    return BackgroundWork(
        run_id=1, kind="terminal_command", label=label, started_at=started_at, ended_at=ended_at
    )


def _part(call_id: str, status: str, work: BackgroundWork | None = None) -> dict[str, object]:
    part: dict[str, object] = {"presentation": "activity",
        "type": "tool-call",
        "toolCallId": call_id,
        "toolName": "execute_terminal",
        "status": status,
    }
    if work is not None:
        part["background"] = {
            "kind": work.kind,
            "label": work.label,
            "startedAt": work.started_at,
            "endedAt": work.ended_at,
        }
    return part


def _messages(*parts: dict[str, object]) -> list[dict[str, object]]:
    return [{"id": "m1", "role": "assistant", "parts": list(parts), "turn": 1, "inherited": False, "outcome": None, "source": None}]


def test_live_entries_win_and_unsettled_placeholders_are_settling_not_dropped():
    done = _work("done", ended_at="2026-01-01T00:01:00+00:00")
    messages = _messages(
        _part("done", "completed", done),
        _part("live", "pending", _work("live")),
        # 结论已到、还没并入（或撤销已出队、结算没写完）：不猜结局，也不让条目消失。
        _part("in-transit", "pending", _work("in-transit")),
        _part("plain", "completed"),
    )
    entries = project_background_tasks(messages, {"live": _work("live")})
    assert [(entry["toolCallId"], entry["status"]) for entry in entries] == [
        ("live", "running"),
        ("in-transit", "settling"),
        ("done", "completed"),
    ]
    assert entries[1]["endedAt"] is None
    assert entries[2]["endedAt"] == done.ended_at
    validate_background_tasks(entries)
    with pytest.raises(ValueError):
        validate_background_tasks([{**entries[0], "mode": "background"}])


def test_live_work_shows_before_its_placeholder_is_written():
    # 并行批次里占位行要等整批结束才写入：part 还是 running、没有记录，名字与开始时间取自登记表。
    entries = project_background_tasks(
        _messages(_part("call-bg", "running")), {"call-bg": _work("build")}
    )
    assert entries == [
        {
            "toolCallId": "call-bg",
            "kind": "terminal_command",
            "label": "build",
            "status": "running",
            "startedAt": _START,
            "endedAt": None,
        }
    ]


def test_active_work_is_ordered_by_start_and_history_keeps_latest_endings():
    parts = [_part("long", "completed", _work("long", ended_at="2026-01-01T01:00:00+00:00"))]
    parts.extend(
        _part(
            f"done-{i}", "completed", _work(f"done-{i}", ended_at=f"2026-01-01T00:{i:02d}:00+00:00")
        )
        for i in range(21)
    )
    live = {
        "late": _work("late", started_at="2026-01-01T00:05:00+00:00"),
        "early": _work("early", started_at="2026-01-01T00:01:00+00:00"),
    }
    parts.extend([_part("late", "running"), _part("early", "running")])
    entries = project_background_tasks(_messages(*parts), live)
    assert [entry["toolCallId"] for entry in entries] == [
        "early",
        "late",
        *(f"done-{i}" for i in range(2, 21)),
        "long",
    ]


def test_hot_projection_matches_cold_rebuild_through_cancellation(
    tmp_path,
    isolated_storage,
    monkeypatch,
):
    """登记即可见；撤销出队后先收尾中，结算后为取消，且与冷重建一致。"""
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    run = depends.get_conversation_run_crud().create(task.id, "running")
    crud = depends.get_conversation_task_context_crud()
    crud.create(
        ConversationTaskContextRecord(
            task_id=task.id,
            run_id=run.id,
            sequence=1,
            include_in_context=True,
            message={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-bg",
                        "type": "function",
                        "function": {"name": "execute_terminal", "arguments": "{}"},
                    }
                ],
            },
            turn=1,
        )
    )
    service = ConversationTaskStateService()
    assert service.get_state(task.id)["background_tasks"] == []
    work = command_results.register(
        "call-bg",
        task_id=task.id,
        run_id=run.id,
        kind="terminal_command",
        label="build",
        on_cancel=lambda _reason: None,
    )
    service.refresh_background_results(task.id)
    assert [entry["status"] for entry in service.get_state(task.id)["background_tasks"]] == [
        "running"
    ]
    crud.create(
        ConversationTaskContextRecord(
            task_id=task.id,
            run_id=run.id,
            sequence=2,
            include_in_context=True,
            message={"role": "tool", "tool_call_id": "call-bg", "content": "placeholder"},
            metadata=ToolMessageMetadata(status="pending", background=work),
            turn=1,
        )
    )
    service.apply_planned(
        ToolCallStatusChangedEvent(
            task_id=task.id,
            run_id=run.id,
            tool_call_id="call-bg",
            status="pending",
            background=work,
        )
    )
    assert service.get_state(task.id)["background_tasks"][0]["status"] == "running"
    assert (
        service.get_state(task.id)["background_tasks"]
        == service.rebuild_state(task.id)["background_tasks"]
    )

    command_results.withdraw(task.id, "call-bg")
    service.refresh_background_results(task.id)
    assert service.get_state(task.id)["background_tasks"][0]["status"] == "settling"
    monkeypatch.setattr(
        depends.get_conversation_event_projector(),
        "process",
        service.apply_planned,
    )
    assert cancel_placeholders(
        task.id, ["call-bg"], "cancelled"
    ) == frozenset({task.id})
    hot = service.get_state(task.id)["background_tasks"]
    assert hot[0]["status"] == "cancelled" and hot[0]["endedAt"] is not None
    assert hot == service.rebuild_state(task.id)["background_tasks"]


def test_withdrawn_rounds_are_not_listed(tmp_path, isolated_storage):
    """编辑撤回的轮次不进工作副本，看板自然不列；规则只在构造 part 时有一份。"""
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    run = depends.get_conversation_run_crud().create(task.id, "completed")
    crud = depends.get_conversation_task_context_crud()
    assistant = ConversationTaskContextRecord(
        task_id=task.id,
        run_id=run.id,
        sequence=1,
        include_in_context=True,
        message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "kept",
                    "type": "function",
                    "function": {"name": "execute_terminal", "arguments": "{}"},
                },
            ],
        },
        turn=1,
    )
    crud.create(assistant)
    placeholder = ConversationTaskContextRecord(
        task_id=task.id,
        run_id=run.id,
        sequence=2,
        include_in_context=True,
        message={"role": "tool", "tool_call_id": "kept", "content": "placeholder"},
        metadata=ToolMessageMetadata(
            status="completed", background=_work("kept", ended_at="2026-01-01T00:01:00+00:00")
        ),
        turn=1,
    )
    crud.create(placeholder)
    withdrawn_call = replace(
        assistant,
        sequence=3,
        include_in_context=False,
        project_to_transport=False,
        message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "gone",
                    "type": "function",
                    "function": {"name": "execute_terminal", "arguments": "{}"},
                },
            ],
        },
    )
    crud.create(withdrawn_call)
    crud.create(
        replace(
            placeholder,
            sequence=4,
            include_in_context=False,
            project_to_transport=False,
            message={"role": "tool", "tool_call_id": "gone", "content": "placeholder"},
        )
    )
    entries = ConversationTaskStateService().get_state(task.id)["background_tasks"]
    assert [entry["toolCallId"] for entry in entries] == ["kept"]
