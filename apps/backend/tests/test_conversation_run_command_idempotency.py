from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandService,
)


def _record(command_id: str, *, payload_hash: str = "hash", run_id: int | None = 7):
    return SimpleNamespace(
        command_id=command_id,
        payload_hash=payload_hash,
        run_id=run_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _service(records: dict[str, object]) -> ConversationRunCommandService:
    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._command = SimpleNamespace(get=lambda _task_id, command_id: records.get(command_id))
    service._run_state = SimpleNamespace(get_run=lambda run_id: SimpleNamespace(id=run_id))
    service._state = SimpleNamespace(get_state=lambda _task_id: {"current_run_id": 7})
    return service


def test_command_batch_replay_requires_every_command_id() -> None:
    service = _service({"message-id": _record("message-id")})

    with pytest.raises(RuntimeError, match="only part of the command batch"):
        service._resolve_existing_command(
            task_id=1,
            command_ids=["message-id", "ban-tools-id"],
            payload_hash="hash",
            mode="new",
        )


def test_command_batch_replay_requires_matching_payload_and_run() -> None:
    service = _service(
        {
            "message-id": _record("message-id"),
            "ban-tools-id": _record("ban-tools-id", run_id=8),
        }
    )

    with pytest.raises(RuntimeError, match="bound to different runs"):
        service._resolve_existing_command(
            task_id=1,
            command_ids=["message-id", "ban-tools-id"],
            payload_hash="hash",
            mode="new",
        )


def test_command_batch_replay_returns_existing_run_when_all_match() -> None:
    service = _service(
        {
            "message-id": _record("message-id"),
            "ban-tools-id": _record("ban-tools-id"),
        }
    )

    result = service._resolve_existing_command(
        task_id=1,
        command_ids=["message-id", "ban-tools-id"],
        payload_hash="hash",
        mode="new",
    )

    assert result is not None
    assert result.created is False
    assert result.run.id == 7

