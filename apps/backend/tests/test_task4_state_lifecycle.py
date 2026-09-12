"""Task 4 RED tests for canonical cold reads and snapshot persistence removal."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.service.task import conversation_run_service as conversation_run_service_module
from app.service.task.conversation_run_service import ConversationRunService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


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


@pytest.fixture(autouse=True)
def _reset_task_runtime_spaces() -> None:
    """Keep each state-service test isolated from the task-local process cache."""

    ConversationTaskStateService.clear_process_state()
    yield
    ConversationTaskStateService.clear_process_state()


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


def test_state_snapshot_is_lazily_rebuilt_once_per_task_runtime_space() -> None:
    ConversationTaskStateService.clear_process_state()
    task_runtime_spaces.close()
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
    reads = {"task": 0, "runs": 0, "context": 0}

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            reads["task" if include_in_context else "context"] += 1
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            reads["runs"] += 1
            return [run]

    try:
        first_service = ConversationTaskStateService(
            task_source=CanonicalSources(),
            run_source=CanonicalSources(),
            context_source=CanonicalSources(),
        )
        second_service = ConversationTaskStateService(
            task_source=CanonicalSources(),
            run_source=CanonicalSources(),
            context_source=CanonicalSources(),
        )

        first = first_service.get_state(7)
        second = first_service.get_state(7)
        third = second_service.get_state(7)

        assert first == second == third
        assert reads == {"task": 1, "runs": 1, "context": 1}
        space = task_runtime_spaces.get(7)
        assert space is not None
        assert space.existing_snapshot() == first
    finally:
        task_runtime_spaces.close()
        ConversationTaskStateService.clear_process_state()


def test_memory_state_is_not_written_to_persistence_and_rebuilds_after_restart() -> None:
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
    first = service.get_state(7)
    assert [run["runId"] for run in first["runs"]] == [2]
    assert service.get_state(7) == first

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


def test_get_state_returns_preinstalled_snapshot_without_rebuild() -> None:
    task = _task()
    run = _run(2, "running")
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message=HumanMessage(content="committed user"),
            include_in_context=True,
            sequence=1,
        ),
        ConversationTaskContextRecord(
            id=12,
            task_id=7,
            run_id=2,
            message=AIMessage(content="committed assistant fact"),
            include_in_context=True,
            sequence=2,
            transport_metadata={
                "schema_version": 1,
                "parts": [
                    {
                        "type": "text",
                        "text": "committed assistant fact",
                        "status": "completed",
                    }
                ],
                "tool_result": None,
            },
        ),
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
    task_runtime_spaces.get_or_create(7).replace_snapshot({
        "runs": [
            {
                "runId": 2,
                "status": "running",
                "endReason": None,
                "messages": [
                    {
                        "id": "stale-user",
                        "role": "user",
                        "parts": [{"type": "text", "text": "stale", "status": "completed"}],
                    },
                    {
                        "id": "stale-assistant",
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "stale", "status": "running"}],
                    },
                ],
                "usage": None,
            }
        ],
        "current_run_id": 2,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    })

    state = service.get_state(7)

    assert [message["id"] for message in state["runs"][0]["messages"]] == [
        "stale-user",
        "stale-assistant",
    ]


@pytest.mark.parametrize("method_name", ["claim_pending_run", "claim_or_resume_run"])
def test_claim_run_is_not_aborted_by_post_commit_projector_failure(
    monkeypatch: pytest.MonkeyPatch, method_name: str
) -> None:
    class RunCrud:
        def update_status_if_in(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(task_id=7)

        def get(self, _run_id: int) -> SimpleNamespace:
            return SimpleNamespace(status="running")

    class FailingProjector:
        def process(self, _event: object) -> None:
            raise RuntimeError("transport unavailable")

    monkeypatch.setattr(
        conversation_run_service_module.service_depends,
        "get_conversation_event_projector",
        lambda: FailingProjector(),
    )
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = RunCrud()

    assert getattr(service, method_name)(7) is True


def test_malformed_context_deserialization_fails_at_state_read_boundary() -> None:
    class MalformedContextSource:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            if include_in_context:
                return _task()
            raise ValueError("malformed persisted context JSON")

    class CanonicalRuns:
        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return []

    service = ConversationTaskStateService(
        task_source=MalformedContextSource(),
        run_source=CanonicalRuns(),
        context_source=MalformedContextSource(),
    )

    with pytest.raises(ValueError, match="malformed persisted context"):
        service.get_state(7)


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
