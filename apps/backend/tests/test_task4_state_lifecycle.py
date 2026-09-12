"""Task 4 RED tests for canonical cold reads and snapshot persistence removal."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord


def _task() -> TaskRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return TaskRecord(
        id=7,
        workspace_id=3,
        title="canonical task",
        created_at=now,
        updated_at=now,
        current_run_id=2,
        context_usage_used=42,
        context_window_total=100,
    )


def _run(run_id: int, status: str) -> ConversationRunRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return ConversationRunRecord(
        id=run_id,
        task_id=7,
        input_text=f"input-{run_id}",
        status=status,
        created_at=now,
        updated_at=now,
        checkpoint_thread_id=f"checkpoint-{run_id}",
    )


def test_cold_state_rebuild_reads_only_task_runs_and_context() -> None:
    task = _task()
    runs = [_run(2, "completed"), _run(1, "failed")]
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message=HumanMessage(content="from canonical context"),
            include_in_context=True,
            sequence=1,
        )
    ]

    class CanonicalSources:
        def get(
            self, task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            assert task_id == 7
            return task if include_in_context else rows

        def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
            assert task_id == 7
            return runs

    sources = CanonicalSources()
    service = ConversationTaskStateService(
        task_source=sources,
        run_source=sources,
        context_source=sources,
    )

    state = service.get_state(7)

    assert state["current_run_id"] == 2
    assert state["context_usage_used"] == 42
    assert state["context_usage_ratio"] == 0.42
    assert [run["runId"] for run in state["runs"]] == [1, 2]
    assert state["runs"][1]["messages"][0]["parts"][0]["text"] == "from canonical context"


def test_memory_state_is_not_written_to_persistence_and_cold_read_rebuilds_after_restart() -> None:
    task = _task()
    run = _run(2, "completed")
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message=HumanMessage(content="durable"),
            include_in_context=True,
            sequence=1,
        )
    ]

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [run]

    service = ConversationTaskStateService(
        task_source=CanonicalSources(),
        run_source=CanonicalSources(),
        context_source=CanonicalSources(),
    )
    service._states[7] = {
        "runs": [],
        "current_run_id": None,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }

    assert [run["runId"] for run in service.get_state(7)["runs"]] == [2]
    assert not hasattr(service, "_crud")

    ConversationTaskStateService.clear_process_state()
    restarted = ConversationTaskStateService(
        task_source=CanonicalSources(),
        run_source=CanonicalSources(),
        context_source=CanonicalSources(),
    )
    rebuilt = restarted.get_state(7)
    assert rebuilt["runs"][0]["messages"][0]["parts"][0]["text"] == "durable"


def test_canonical_state_sources_failures_are_logged_and_not_replaced_by_empty_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenSources:
        def get(self, _task_id: int) -> TaskRecord:
            raise RuntimeError("canonical read failed")

    service = ConversationTaskStateService(
        task_source=BrokenSources(),
        run_source=BrokenSources(),
        context_source=BrokenSources(),
    )

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="canonical read failed"):
        service.get_state(7)

    assert "state_rebuild_started" in {record.message for record in caplog.records}
    assert "state_rebuild_failed" in {record.message for record in caplog.records}


def test_persisted_snapshot_model_and_crud_are_removed() -> None:
    backend_app = Path(__file__).parents[1] / "app"
    assert not (backend_app / "storage/model/conversation_task_snapshot_model.py").exists()
    assert not (backend_app / "storage/crud/conversation_task_snapshot_crud.py").exists()


@pytest.mark.asyncio
async def test_cold_sse_first_frame_uses_canonical_rebuild() -> None:
    """The first SSE frame must come from canonical records when no working copy exists."""

    ConversationTaskStateService.clear_process_state()

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return _task() if include_in_context else []

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [_run(2, "completed")]

    state_service = ConversationTaskStateService(
        task_source=CanonicalSources(),
        run_source=CanonicalSources(),
        context_source=CanonicalSources(),
    )
    stream_service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    stream_service._snapshots = state_service

    stream = stream_service.stream(7, 2, lambda: False)
    first = await anext(stream)

    assert first.state["current_run_id"] == 2
    assert first.state["runs"][0]["status"] == "completed"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
