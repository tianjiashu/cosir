import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.init_schema import initialize_app_schema
from app.storage.model.base import StorageBase
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.task_model import TaskModel


def _timestamp() -> datetime:
    return datetime.now(UTC)


def test_context_record_round_trips_row_id_metadata_and_schema_version() -> None:
    record = ConversationTaskContextRecord(
        task_id=7,
        run_id=11,
        message=HumanMessage(content="hello"),
        include_in_context=True,
        sequence=3,
        id=41,
        transport_metadata={"parts": [{"type": "text"}]},
        message_schema_version=2,
    )

    model = record._to_model()
    restored = ConversationTaskContextRecord._from_model(model)

    assert model.id == 41
    assert restored.id == 41
    assert restored.message == record.message
    assert restored.transport_metadata == {"parts": [{"type": "text"}]}
    assert restored.message_schema_version == 2
    assert json.loads(model.transport_metadata_json) == {"parts": [{"type": "text"}]}


def test_context_record_rejects_non_object_metadata_json() -> None:
    model = ConversationTaskContextModel(
        task_id=7,
        run_id=11,
        message_json=json.dumps({"type": "human", "data": {"content": "hello"}}),
        include_in_context=True,
        sequence=3,
        transport_metadata_json="[]",
        message_schema_version=1,
    )

    with pytest.raises((TypeError, ValueError)):
        ConversationTaskContextRecord._from_model(model)


def test_run_record_round_trips_usage_and_error() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cache_hit_tokens": 2,
        "cache_miss_tokens": 8,
        "reasoning_tokens": 1,
    }
    error = {"code": "provider_error", "retryable": True}
    record = ConversationRunRecord(
        id=11,
        task_id=7,
        input_text="run input",
        status="failed",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        checkpoint_thread_id="thread-11",
        usage=usage,
        error=error,
    )

    model = record.to_model()
    restored = ConversationRunRecord.from_model(model)

    assert json.loads(model.usage_json) == usage
    assert json.loads(model.error_json) == error
    assert restored.usage == usage
    assert restored.error == error


def test_run_crud_clone_preserves_usage_and_error() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cache_hit_tokens": 2,
        "cache_miss_tokens": 8,
        "reasoning_tokens": 1,
    }
    source = ConversationRunRecord(
        id=11,
        task_id=7,
        input_text="run input",
        status="failed",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        checkpoint_thread_id="thread-11",
        usage=usage,
        error={"code": "provider_error"},
    )
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            cloned = ConversationRunCrud.clone_for_task(session, source, target_task_id=8)

        assert cloned.task_id == 8
        assert cloned.usage == usage
        assert cloned.error == {"code": "provider_error"}
    finally:
        engine.dispose()


def test_task_record_round_trips_current_run_and_context_window() -> None:
    now = _timestamp()
    model = TaskModel(
        id=7,
        workspace_id=3,
        title="task",
        task_type="user",
        current_run_id=11,
        context_usage_used=90,
        context_window_total=128,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    record = TaskRecord.from_model(model)

    assert record.current_run_id == 11
    assert record.context_usage_used == 90
    assert record.context_window_total == 128
    assert record.to_dict()["current_run_id"] == 11
    assert record.to_dict()["context_window_total"] == 128
    round_tripped = record.to_model()
    assert round_tripped.current_run_id == 11
    assert round_tripped.context_window_total == 128


def test_task_crud_new_task_has_empty_run_and_window_defaults() -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = TaskCrud.__new__(TaskCrud)
        with Session(engine) as session:
            record = crud._build_and_flush(
                session,
                workspace_id=3,
                title="empty",
                task_type="user",
                parent_task_id=None,
                parent_run_id=None,
                delegation_id=None,
                creation_command_id=None,
                extra=None,
            )

        assert record.context_usage_used == 0
        assert record.current_run_id is None
        assert record.context_window_total is None
    finally:
        engine.dispose()


def test_fresh_app_schema_does_not_create_task_snapshots_table() -> None:
    engine = create_engine("sqlite://")
    try:
        initialize_app_schema(engine)
        assert "conversation_task_snapshots" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
