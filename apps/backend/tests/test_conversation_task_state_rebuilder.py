"""Rebuild-boundary tests for the canonical Transport snapshot assembler."""

from datetime import UTC, datetime

import pytest

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.models.task_record import TaskRecord


def _timestamp() -> datetime:
    return datetime(2026, 9, 12, tzinfo=UTC)


def _task(task_id: int = 7) -> TaskRecord:
    timestamp = _timestamp()
    return TaskRecord(
        id=task_id,
        workspace_id=3,
        title="task",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_rebuild_empty_task_returns_valid_empty_snapshot() -> None:
    state = ConversationTaskStateRebuilder.rebuild(_task(), [], [])

    assert state == {
        "runs": [],
        "current_run_id": None,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }


def test_validate_snapshot_rejects_current_run_that_is_not_present() -> None:
    invalid_state = {
        "runs": [],
        "current_run_id": 99,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }

    with pytest.raises(ValueError, match="current_run_id must refer to a run"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]
