"""子 Task 最新 Run 状态投影到父任务委派卡片的回归：卡片按 child_task_id 定位，不跟踪某条 Run。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
    _touched_message_indexes,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.core.events import RunInitializedEvent, ToolCallStatusChangedEvent
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata
from app.models.task_record import TaskRecord
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.model.base import StorageBase
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel

pytestmark = pytest.mark.usefixtures("isolated_storage")

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _task(
    task_id: int,
    *,
    parent_task_id: int | None = None,
    current_run_id: int | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        workspace_id=1,
        title=f"task-{task_id}",
        created_at=NOW,
        updated_at=NOW,
        parent_task_id=parent_task_id,
        current_run_id=current_run_id,
    )


def _run(run_id: int, task_id: int, status: str) -> ConversationRunRecord:
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def _tool_rows(*displays: tuple[str, dict[str, object]]) -> list[ConversationTaskContextRecord]:
    calls = [
        {
            "id": tool_call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps({})},
        }
        for tool_call_id, tool_name in (
            ("delegate-call", "delegate_task"),
            ("send-call", "child_agent_send"),
            ("status-call", "child_agent_status"),
        )
    ]
    rows: list[ConversationTaskContextRecord] = [
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message={"role": "assistant", "content": "", "tool_calls": calls},
            include_in_context=True,
            sequence=1,
            metadata={},
        )
    ]
    for sequence, (call, (_, display)) in enumerate(
        zip(calls, displays, strict=True),
        start=2,
    ):
        tool_call_id = call["id"]
        rows.append(
            ConversationTaskContextRecord(
                task_id=1,
                run_id=10,
                message={"role": "tool", "content": "result", "tool_call_id": tool_call_id},
                include_in_context=True,
                sequence=sequence,
                metadata=ToolMessageMetadata.model_validate(
                    {
                        "status": "completed",
                        "display_data": display,
                    }
                ),
            )
        )
    return rows


class _Sources:
    def __init__(self, rows: list[ConversationTaskContextRecord]) -> None:
        self.tasks = {
            1: _task(1, current_run_id=10),
            2: _task(2, parent_task_id=1, current_run_id=21),
        }
        self.runs = {
            10: _run(10, 1, "completed"),
            20: _run(20, 2, "completed"),
            21: _run(21, 2, "running"),
        }
        self.rows = rows
        self.metadata_updates: list[tuple[int, int, str]] = []

    def get(
        self,
        task_id: int,
        include_in_context: bool = True,
    ) -> TaskRecord | list[ConversationTaskContextRecord]:
        if include_in_context:
            return self.tasks[task_id]
        return list(self.rows)

    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
        return [run for run in self.runs.values() if run.task_id == task_id]

    def update_child_display_status(
        self,
        task_id: int,
        sequence: int,
        child_task_id: int,
        status: str,
    ) -> None:
        self.metadata_updates.append((task_id, sequence, status))
        for index, row in enumerate(self.rows):
            if row.task_id == task_id and row.sequence == sequence:
                metadata = row.metadata.model_dump()
                display = dict(metadata.get("display_data") or {})
                if display.get("child_task_id") == child_task_id:
                    display["status"] = status
                    metadata["display_data"] = display
                    self.rows[index] = replace(row, metadata=metadata)


class _RunSource:
    """Run 读取替身：``get`` 与 Task / context 读取同名，单独成对象避免签名冲突。"""

    def __init__(self, sources: _Sources) -> None:
        self._sources = sources

    def get(self, run_id: int) -> ConversationRunRecord:
        return self._sources.runs[run_id]

    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
        return self._sources.list_by_task(task_id)


def _service(sources: _Sources) -> ConversationTaskStateService:
    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    service._task_source = sources
    service._run_source = _RunSource(sources)
    service._context_source = sources
    return service


def _display_by_call(state: dict[str, object], tool_call_id: str) -> dict[str, object]:
    for message in state["messages"]:  # type: ignore[index]
        for part in message["parts"]:
            if part.get("toolCallId") == tool_call_id:
                return part["display_data"]
    raise AssertionError(f"tool call not found: {tool_call_id}")


@pytest.fixture(autouse=True)
def _reset_task_runtime_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )

    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_task_service",
        lambda: SimpleNamespace(get_task=lambda task_id: _task(task_id)),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=1)),
    )
    ConversationTaskStateService.clear_process_state()
    yield
    ConversationTaskStateService.clear_process_state()


def test_cold_rebuild_shows_child_task_latest_run_status() -> None:
    rows = _tool_rows(
        (
            "delegate-call",
            {
                "kind": "delegation-result",
                "title": "Review",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "send-call",
            {
                "kind": "child-agent-result",
                "operation": "send",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "status-call",
            {
                "kind": "child-agent-result",
                "operation": "status",
                "child_task_id": 2,
                "status": "running",
            },
        ),
    )
    sources = _Sources(rows)
    sources.runs[21].status = "failed"

    state = _service(sources).get_state(1)

    # 委派卡片与追加消息卡片都展示子 Task 最新 Run 的状态，与委派时是哪条 Run 无关。
    assert _display_by_call(state, "delegate-call")["status"] == "failed"
    assert _display_by_call(state, "send-call")["status"] == "failed"
    # status is a point-in-time query result, not a live child status card.
    assert _display_by_call(state, "status-call")["status"] == "running"
    assert sources.metadata_updates == []


def test_live_refresh_follows_child_task_latest_run_and_ignores_old_run_events() -> None:
    rows = _tool_rows(
        (
            "delegate-call",
            {
                "kind": "delegation-result",
                "title": "Review",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "send-call",
            {
                "kind": "child-agent-result",
                "operation": "send",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "status-call",
            {
                "kind": "child-agent-result",
                "operation": "status",
                "child_task_id": 2,
                "status": "running",
            },
        ),
    )
    sources = _Sources(rows)
    service = _service(sources)
    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "running"

    # 旧 Run 的迟到事件不改卡片：卡片只跟子 Task 的最新 Run。
    assert service.refresh_parent_delegation(2, 20, "completed") is None
    assert sources.metadata_updates == []

    sources.runs[21].status = "cancelled"
    service.refresh_parent_delegation(2, 21, "running")
    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "cancelled"
    assert _display_by_call(state, "send-call")["status"] == "cancelled"
    assert _display_by_call(state, "status-call")["status"] == "running"
    assert [sequence for _, sequence, _ in sources.metadata_updates] == [2, 3]
    assert {status for _, _, status in sources.metadata_updates} == {"cancelled"}


def test_live_refresh_follows_a_suspended_child_run() -> None:
    """子 Agent 挂起（等审批 / 等消息）时委派卡片也要跟上，不能停在 running。"""
    rows = _tool_rows(
        (
            "delegate-call",
            {
                "kind": "delegation-result",
                "title": "Review",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "send-call",
            {
                "kind": "child-agent-result",
                "operation": "send",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "status-call",
            {
                "kind": "child-agent-result",
                "operation": "status",
                "child_task_id": 2,
                "status": "running",
            },
        ),
    )
    sources = _Sources(rows)
    service = _service(sources)
    service.get_state(1)

    sources.runs[21].status = "suspended"
    service.refresh_parent_delegation(2, 21, "suspended")

    state = service.get_state(1)
    assert _display_by_call(state, "delegate-call")["status"] == "suspended"
    assert _display_by_call(state, "send-call")["status"] == "suspended"
    assert {status for _, _, status in sources.metadata_updates} == {"suspended"}


def test_event_projection_persists_status_when_parent_tool_row_arrives_after_child_completion() -> (
    None
):
    rows = _tool_rows(
        (
            "delegate-call",
            {
                "kind": "delegation-result",
                "title": "Review",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "send-call",
            {
                "kind": "child-agent-result",
                "operation": "send",
                "child_task_id": 2,
                "status": "running",
            },
        ),
        (
            "status-call",
            {
                "kind": "child-agent-result",
                "operation": "status",
                "child_task_id": 2,
                "status": "running",
            },
        ),
    )
    sources = _Sources(rows)
    service = _service(sources)
    service.get_state(1)

    sources.runs[21].status = "completed"
    # 父任务的工具卡片写入（这一批改写了卡片所在的消息）时对账，补上早结束的子 Run 状态。
    change = service.apply_planned(ToolCallStatusChangedEvent(
        task_id=1,
        run_id=10,
        tool_call_id="delegate-call",
        status="completed",
        display_data={"kind": "delegation-result", "title": "Review", "child_task_id": 2, "status": "running"},
    ))

    assert change.mutations
    assert _display_by_call(service.get_state(1), "delegate-call")["status"] == "completed"
    assert [sequence for _, sequence, _ in sources.metadata_updates] == [2, 3]


def test_events_that_touch_no_message_skip_child_status_reconcile() -> None:
    """对账只看本批改写过的消息：不碰消息的事件（及流式追加）不扫历史、不查库。"""
    rows = _tool_rows(
        ("delegate-call", {"kind": "delegation-result", "title": "Review", "child_task_id": 2, "status": "running"}),
        ("send-call", {"kind": "child-agent-result", "operation": "send", "child_task_id": 2, "status": "running"}),
        ("status-call", {"kind": "child-agent-result", "operation": "status", "child_task_id": 2, "status": "running"}),
    )
    sources = _Sources(rows)
    service = _service(sources)
    service.get_state(1)

    sources.runs[21].status = "completed"
    service.apply_planned(RunInitializedEvent(task_id=1, run_id=10))

    assert _display_by_call(service.get_state(1), "delegate-call")["status"] == "running"
    assert sources.metadata_updates == []


def test_touched_message_indexes_ignores_text_appends() -> None:
    def mutation(kind: str, *path: str | int) -> ConversationStateMutation:
        return ConversationStateMutation(kind, path, "x")  # type: ignore[arg-type]

    assert _touched_message_indexes([mutation("append-text", "messages", 4, "parts", 0, "text")]) == set()
    assert _touched_message_indexes([
        mutation("set", "messages", 3, "parts", 1, "display_data"),
        mutation("set", "activity"),
    ]) == {3}
    assert _touched_message_indexes([mutation("set", "messages")]) is None
    assert _touched_message_indexes([mutation("set")]) is None


def test_context_crud_updates_only_child_display_status_and_keeps_concurrent_metadata() -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        metadata = {
            "status": "completed",
            "display_data": {
                "kind": "delegation-result",
                "child_task_id": 2,
                "status": "running",
            },
            "team_run_id": 77,
        }
        with Session(engine) as session:
            session.add(
                ConversationTaskContextModel(
                    message_id="test-message",
                    task_id=1,
                    run_id=10,
                    message_json=json.dumps(
                        {"role": "tool", "tool_call_id": "call-1", "content": "old"}
                    ),
                    include_in_context=True,
                    sequence=2,
                    metadata_json=json.dumps(metadata),
                )
            )
            session.flush()

            crud.update_child_display_status(1, 2, 2, "failed", session=session)
            row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 1,
                    ConversationTaskContextModel.sequence == 2,
                )
            )
            assert row is not None
            updated = json.loads(row.metadata_json)
            assert updated["display_data"]["status"] == "failed"
            assert updated["team_run_id"] == 77

            crud.replace_message(
                1,
                2,
                ConversationTaskContextRecord(
                    task_id=1,
                    run_id=10,
                    message={"role": "tool", "tool_call_id": "call-1", "content": "new"},
                    metadata=ToolMessageMetadata(status="cancelled"),
                    include_in_context=True,
                    sequence=2,
                ),
                session=session,
            )
            row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 1,
                    ConversationTaskContextModel.sequence == 2,
                )
            )
            assert row is not None
            assert (
                json.loads(row.metadata_json)
                == ToolMessageMetadata(status="cancelled").model_dump()
            )
    finally:
        engine.dispose()


# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")
