import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.assistant_transport.event import RunStatusChangedEvent
from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_extra import ConversationRunExtra
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.conversation_run.conversation_task_context_service import ConversationTaskContextService
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.model.base import StorageBase
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.task_model import TaskModel
from app.storage.store_engines import initialize_app_schema


def _timestamp() -> datetime:
    return datetime.now(UTC)


def test_context_record_round_trips_with_row_identity_and_transport_metadata() -> None:
    metadata = {
        "schema_version": 1,
        "parts": [{"type": "text", "text": "hello", "status": "completed"}],
        "tool_result": {
            "status": "success",
            "display_data": {"kind": "file"},
            "status_hint": None,
            "error": None,
        },
    }
    record = ConversationTaskContextRecord(
        task_id=7,
        run_id=11,
        message=HumanMessage(content="hello"),
        include_in_context=True,
        sequence=3,
        id=41,
        transport_metadata=metadata,
    )

    model = record._to_model()
    restored = ConversationTaskContextRecord._from_model(model)

    assert model.id == 41
    assert restored.id == 41
    assert restored.message == record.message
    assert restored.transport_metadata == metadata
    assert json.loads(model.transport_metadata_json) == metadata


def test_context_storage_exposes_only_canonical_message_fields() -> None:
    assert {column.name for column in ConversationTaskContextModel.__table__.columns} == {
        "id",
        "task_id",
        "run_id",
        "message_json",
        "transport_metadata_json",
        "include_in_context",
        "is_streaming",
        "sequence",
        "created_at",
        "updated_at",
    }


def test_context_record_rejects_malformed_metadata_json() -> None:
    model = ConversationTaskContextModel(
        task_id=7,
        run_id=11,
        message_json=json.dumps({"type": "human", "data": {"content": "hello"}}),
        include_in_context=True,
        sequence=3,
        transport_metadata_json="{not-json",
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
    error = {"code": "provider_error", "message": "provider unavailable"}
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


def test_run_extra_serializes_direct_shape_without_version() -> None:
    extra = ConversationRunExtra(
        display_text="请阅读 [[cosir-file:readme.md]]",
        attachments=[
            {
                "id": "readme.md",
                "name": "README.md",
                "content_type": "text/markdown",
                "path": "attachments/readme.md",
            }
        ],
        reasoning_effort="high",
        langfuse_trace_id="trace-11",
    )

    serialized = extra.to_dict()
    assert serialized == {
        "display_text": "请阅读 [[cosir-file:readme.md]]",
        "attachments": [
            {
                "id": "readme.md",
                "name": "README.md",
                "content_type": "text/markdown",
                "path": "attachments/readme.md",
            }
        ],
        "ban_tools": [],
        "reasoning_effort": "high",
        "langfuse_trace_id": "trace-11",
    }
    assert "version" not in serialized
    assert ConversationRunExtra.from_dict(serialized) == extra


def test_run_extra_rejects_legacy_assistant_input_wrapper() -> None:
    with pytest.raises((TypeError, ValueError)):
        ConversationRunExtra.from_dict(
            {
                "assistant_input": {
                    "version": 1,
                    "display_text": "请阅读 [[cosir-file:readme.md]]",
                    "attachments": [],
                }
            }
        )


def test_run_service_prepares_new_command_for_model_and_persistence() -> None:
    attachment_path = Path(__file__)
    service = ConversationRunService.__new__(ConversationRunService)
    service._task = SimpleNamespace(
        get=lambda _task_id: SimpleNamespace(
            tool_schemas=[
                {"name": "read_file"},
                {"name": "web_search"},
                {"name": "propose_agent_configuration"},
            ]
        )
    )
    command = ConversationRunCommand(
        display_text="请阅读 [[cosir-file:readme]]",
        attachments=[
            ConversationRunAttachmentInput(
                id="readme",
                name="README.md",
                content_type="text/markdown",
                path=str(attachment_path),
            )
        ],
    )

    prepared = service._prepare_command(  # type: ignore[attr-defined]
        7,
        command,
        run_id=None,
        supports_image=False,
        reasoning_effort=None,
    )

    assert prepared.input_text == f"请阅读 {attachment_path}"
    assert prepared.extra is not None
    assert prepared.extra.display_text == command.display_text
    assert prepared.extra.attachments[0]["id"] == "readme"
    assert prepared.extra.ban_tools == []


def test_run_service_delays_banned_tools_to_task_resolution() -> None:
    service = ConversationRunService.__new__(ConversationRunService)
    service._task = SimpleNamespace(
        get=lambda _task_id: SimpleNamespace(
            tool_schemas=[
                {"name": "execute_terminal"},
                {"name": "web_search"},
                {"name": "propose_agent_configuration"},
            ]
        )
    )
    command = ConversationRunCommand(
        display_text="检查项目",
        ban_tools=["execute_terminal"],
    )

    prepared = service._prepare_command(  # type: ignore[attr-defined]
        7,
        command,
        run_id=None,
        supports_image=False,
        reasoning_effort=None,
    )

    assert prepared.extra is not None
    assert prepared.extra.attachments == []
    assert prepared.extra.ban_tools == ["execute_terminal"]
    assert prepared.extra.to_dict()["ban_tools"] == ["execute_terminal"]


def test_run_service_accepts_directory_as_one_ordinary_attachment() -> None:
    attachment_path = Path(__file__).parent
    service = ConversationRunService.__new__(ConversationRunService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(tool_schemas=[]))
    command = ConversationRunCommand(
        display_text="请检查 [[cosir-file:references]]",
        attachments=[
            ConversationRunAttachmentInput(
                id="references",
                name="references",
                content_type="application/x-directory",
                path=str(attachment_path),
            )
        ],
    )

    prepared = service._prepare_command(  # type: ignore[attr-defined]
        7,
        command,
        run_id=None,
        supports_image=False,
        reasoning_effort=None,
    )

    assert prepared.input_text == f"请检查 {attachment_path}"
    assert prepared.extra is not None
    assert prepared.extra.attachments == [
        {
            "id": "references",
            "name": "references",
            "content_type": "application/x-directory",
            "path": str(attachment_path),
        }
    ]


def test_run_service_prepares_edit_command_from_existing_attachment() -> None:
    attachment_path = Path(__file__)
    existing_extra = ConversationRunExtra(
        display_text="请阅读 [[cosir-file:readme]]",
        attachments=[
            {
                "id": "readme",
                "name": "README.md",
                "content_type": "text/markdown",
                "path": str(attachment_path),
            }
        ],
        reasoning_effort=None,
    )
    service = ConversationRunService.__new__(ConversationRunService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(tool_schemas=[]))
    service._run = SimpleNamespace(get=lambda _run_id: SimpleNamespace(extra=existing_extra))
    command = ConversationRunCommand(
        display_text="请再次阅读 [[cosir-file:readme]]",
        attachments=[
            ConversationRunAttachmentInput(
                id="readme",
                name="README.md",
                content_type="text/markdown",
            )
        ],
    )

    prepared = service._prepare_command(  # type: ignore[attr-defined]
        7,
        command,
        run_id=11,
        supports_image=False,
        reasoning_effort=None,
    )

    assert prepared.input_text == f"请再次阅读 {attachment_path}"
    assert prepared.extra is not None
    assert prepared.extra.attachments[0]["path"] == str(attachment_path)


def test_run_crud_clone_for_fork_preserves_facts_with_independent_checkpoint() -> None:
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
        extra=ConversationRunExtra(
            display_text="run input",
            attachments=[],
            langfuse_trace_id="trace-source",
        ),
        usage=usage,
        error={"code": "provider_error", "message": "provider unavailable"},
    )
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            cloned = ConversationRunCrud.clone_for_task(session, source, target_task_id=8)

        assert cloned.task_id == 8
        assert cloned.usage == usage
        assert cloned.error == {
            "code": "provider_error",
            "message": "provider unavailable",
        }
        assert cloned.checkpoint_thread_id != source.checkpoint_thread_id
        assert cloned.extra is not None
        assert cloned.extra.langfuse_trace_id is None
    finally:
        engine.dispose()


def test_context_clone_copies_transport_fields_to_real_row() -> None:
    metadata = {
        "schema_version": 1,
        "parts": [{"type": "text", "text": "source", "status": "completed"}],
        "tool_result": {
            "status": "success",
            "display_data": {"kind": "file"},
            "status_hint": None,
            "error": None,
        },
    }
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        service = ConversationTaskContextService.__new__(ConversationTaskContextService)
        service._crud = crud
        with Session(engine) as session:
            session.add(
                ConversationTaskContextRecord(
                    task_id=7,
                    run_id=None,
                    message=SystemMessage(content="fixed system prompt"),
                    include_in_context=True,
                    sequence=0,
                )._to_model()
            )
            session.add(
                ConversationTaskContextModel(
                    id=41,
                    task_id=7,
                    run_id=11,
                    message_json=json.dumps({"type": "human", "data": {"content": "source"}}),
                    include_in_context=True,
                    sequence=3,
                    transport_metadata_json=json.dumps(metadata),
                )
            )
            session.flush()

            service.clone_for_fork(7, 8, {11: 22}, session)
            cloned_row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 8,
                    ConversationTaskContextModel.sequence == 3,
                )
            )

            assert cloned_row is not None
            assert cloned_row.run_id == 22
            assert json.loads(cloned_row.transport_metadata_json) == metadata
    finally:
        engine.dispose()


def test_context_crud_raises_on_sequence_collision() -> None:
    """序号冲突（调用方分配序号出错）必须原样抛出，不得在持久化层被吞掉。

    工具调用身份不再是数据库事实（``tool_call_id`` 列已删除），因此 ``create`` 的唯一冲突来源
    只有 ``(task_id, sequence)``；它代表序号 owner 出错，只能暴露。
    """

    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        first = ConversationTaskContextRecord(
            task_id=7,
            run_id=11,
            message=ToolMessage(content="done", tool_call_id="call-1", status="success"),
            include_in_context=True,
            sequence=1,
        )
        conflicting_sequence = ConversationTaskContextRecord(
            task_id=7,
            run_id=11,
            message=ToolMessage(content="other", tool_call_id="call-2", status="success"),
            include_in_context=True,
            sequence=1,
        )
        with Session(engine) as session:
            crud.create(first, session=session)
            with pytest.raises(IntegrityError):
                crud.create(conflicting_sequence, session=session)
    finally:
        engine.dispose()


def test_streaming_draft_finalize_replaces_row_in_place_without_new_row() -> None:
    """收口是原地替换草稿行：行数与 sequence 不变，只翻转 include_in_context / is_streaming。

    这是「同一条 assistant 消息只占一行」的持久化层不变量：流式草稿行被收口后不允许再新增
    第二行（历史缺陷：``flush`` 原地固化 + ``add_message`` 追加修订版，两行都进模型上下文）。
    """

    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        draft = ConversationTaskContextRecord(
            task_id=7,
            run_id=11,
            message=AIMessage(content="半截草稿"),
            include_in_context=False,
            sequence=1,
            is_streaming=True,
        )
        finalized = ConversationTaskContextRecord(
            task_id=7,
            run_id=11,
            message=AIMessage(
                content="修订后的完整回复",
                tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
            ),
            include_in_context=True,
            sequence=1,
            is_streaming=False,
        )
        with Session(engine) as session:
            crud.create(draft, session=session)

            crud.replace_message(7, 1, finalized, session=session)

            rows = crud.get(7, include_in_context=False, session=session)
            assert len(rows) == 1
            assert rows[0].sequence == 1
            assert rows[0].run_id == 11
            assert rows[0].include_in_context is True
            assert rows[0].is_streaming is False
            assert rows[0].message.content == "修订后的完整回复"
            # LangChain 反序列化会给 tool_call 补 type 键，故按字段断言而不是整体相等。
            tool_calls = rows[0].message.tool_calls
            assert len(tool_calls) == 1
            assert tool_calls[0]["name"] == "read_file"
            assert tool_calls[0]["args"] == {"path": "a.py"}
            assert tool_calls[0]["id"] == "call-1"
    finally:
        engine.dispose()


def test_real_orphan_recovery_persists_run_and_interrupted_tool_repair(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with Session(engine) as session:
            session.add(TaskModel(id=7, workspace_id=3, title="task", creation_command_id=None))
            session.add(
                ConversationRunModel(
                    id=11,
                    task_id=7,
                    input_text="hello",
                    status="running",
                    checkpoint_thread_id="thread-11",
                )
            )
            context = ConversationTaskContextRecord(
                task_id=7,
                run_id=11,
                message=AIMessage(
                    content="I will inspect it",
                    tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
                ),
                include_in_context=True,
                sequence=1,
            )
            session.add(context._to_model())
            session.commit()

        run_crud = ConversationRunCrud.__new__(ConversationRunCrud)
        run_crud._session_factory = factory
        context_service = ConversationTaskContextService.__new__(ConversationTaskContextService)
        context_crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        context_crud._session_factory = factory
        context_service._crud = context_crud
        service = ConversationRunService.__new__(ConversationRunService)
        service._run = run_crud
        service._context = context_service
        service._session_factory = factory
        projected: list[RunStatusChangedEvent] = []

        class _Projector:
            def process(self, event: RunStatusChangedEvent) -> None:
                projected.append(event)

        monkeypatch.setattr(
            "app.service.depends.get_conversation_event_projector", lambda: _Projector()
        )

        recovered = service.recover_orphaned_runs()

        with Session(engine) as session:
            run_row = session.get(ConversationRunModel, 11)
            context_rows = session.scalars(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 7,
                    ConversationTaskContextModel.run_id == 11,
                )
            ).all()
            repaired = [
                record
                for record in (
                    ConversationTaskContextRecord._from_model(row) for row in context_rows
                )
                if isinstance(record.message, ToolMessage)
            ]

        assert len(recovered) == 1
        assert run_row is not None
        assert run_row.status == "cancelled"
        assert run_row.end_reason == "runtime_restarted"
        assert len(repaired) == 1
        assert repaired[0].message.tool_call_id == "call-1"
        assert repaired[0].run_id == 11
        # 统一收口为取消：占位与 Run 终态同口径，冷读重建据此得到 cancelled tool part。
        assert repaired[0].transport_metadata == {"status": "cancelled"}
        # 不处理快照：恢复路径不投影任何事件。
        assert projected == []
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
        context_window_total=128,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    record = TaskRecord.from_model(model)

    assert record.current_run_id == 11
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
                creation_command_id=None,
                extra=None,
            )

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
