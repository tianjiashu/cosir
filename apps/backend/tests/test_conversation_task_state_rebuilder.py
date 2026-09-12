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


def test_get_tool_display_returns_empty_dict_for_missing_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 未注册工具 / 无 display 时返回 {} 而非 None，保证重建快照的 presentation
    # 是合法对象、能通过 validate_snapshot（修复前返回 None 会让整张快照冷读崩）。

    class _FakeRegistry:
        def get_tool_definition(self, _name: str):
            return None

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRegistry(),
    )
    assert ConversationTaskStateRebuilder.get_tool_display("unknown_tool") == {}


def test_validate_snapshot_tolerates_none_presentation() -> None:
    # 防御性：presentation 为显式 None 视作缺省，不再使整张快照校验崩溃
    # （旧实现 ``isinstance(part.get("presentation", {}), dict)`` 会命中 None）。
    state = {
        "runs": [
            {
                "runId": 1,
                "status": "completed",
                "endReason": None,
                "usage": None,
                "messages": [
                    {
                        "id": "m1",
                        "role": "user",
                        "parts": [
                            {
                                "type": "tool-call",
                                "toolCallId": "c1",
                                "toolName": "read_file",
                                "status": "cancelled",
                                "presentation": None,
                            }
                        ],
                    }
                ],
            }
        ],
        "current_run_id": 1,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }

    validate_snapshot(state)  # 不应抛 ValueError
