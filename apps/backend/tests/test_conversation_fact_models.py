import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.context.context_store import ContextStore
from app.core.events import RunStatusChangedEvent
from app.models.conversation_input import ConversationInput
from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_options import ConversationRunOptions
from app.models.react_execution_state import ReactExecutionState
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_metadata import ToolMessageMetadata
from app.models.task_record import TaskRecord
from app.service.conversation_run import user_input_service as user_input_module
from app.service.recover_service import RecoverService
from app.core.conversation_run_ledger import (
    ConversationRunLedger,
)
from app.service.conversation_run.conversation_task_context_service import (
    ConversationTaskContextService,
)
from app.service.conversation_run.user_input_service import UserInputService
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


def test_context_record_round_trips_with_row_identity_and_metadata() -> None:
    metadata = {"status": "completed", "display_data": {"kind": "read-file-meta", "path": "a.py"}}
    record = ConversationTaskContextRecord(
        task_id=7,
        run_id=11,
        message={"role": "tool", "tool_call_id": "call-1", "content": "hello"},
        include_in_context=True,
        sequence=3,
        id=41,
        metadata=metadata,
    )

    model = record._to_model()
    restored = ConversationTaskContextRecord._from_model(model)

    assert model.id == 41
    assert restored.id == 41
    assert restored.message_id == record.message_id
    assert restored.message == record.message
    assert restored.metadata == ToolMessageMetadata.model_validate(metadata)
    assert json.loads(model.metadata_json) == ToolMessageMetadata.model_validate(
        metadata
    ).model_dump(mode="json")


def test_context_storage_exposes_only_canonical_message_fields() -> None:
    assert {column.name for column in ConversationTaskContextModel.__table__.columns} == {
        "id",
        "task_id",
        "run_id",
        "message_json",
        "message_id",
        "metadata_json",
        "project_to_transport",
        "include_in_context",
        "sequence",
        "created_at",
        "updated_at",
    }


def test_context_record_rejects_malformed_metadata_json() -> None:
    model = ConversationTaskContextModel(
        message_id="test-message",
        task_id=7,
        run_id=11,
        message_json=json.dumps({"role": "user", **{"content": "hello"}}),
        include_in_context=True,
        sequence=3,
        metadata_json="{not-json",
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
        status="failed",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        usage=usage,
        error=error,
    )

    model = record.to_model()
    restored = ConversationRunRecord.from_model(model)

    assert json.loads(model.usage_json) == usage
    assert json.loads(model.error_json) == error
    assert restored.usage == usage
    assert restored.error == error


def test_input_serializes_separately_from_run_options() -> None:
    message = ConversationInput(
        input_text="请阅读 attachments/readme.md",
        display_text="请阅读 [[cosir-file:readme.md]]",
        attachments=[
            {
                "id": "readme.md",
                "name": "README.md",
                "content_type": "text/markdown",
                "path": "attachments/readme.md",
            }
        ],
    )

    serialized = message.model_dump(mode="json")
    assert "reasoning_effort" not in serialized
    assert "ban_tools" not in serialized
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
        "input_text": "请阅读 attachments/readme.md",
        "image_paths": [],
    }
    assert "version" not in serialized
    assert ConversationInput.model_validate(serialized) == message


def test_input_rejects_legacy_assistant_input_wrapper() -> None:
    with pytest.raises((TypeError, ValueError)):
        ConversationInput.model_validate(
            {
                "assistant_input": {
                    "version": 1,
                    "display_text": "请阅读 [[cosir-file:readme.md]]",
                    "attachments": [],
                }
            }
        )


def test_presentation_rejects_image_token_without_uploaded_image() -> None:
    """正文引用的图片必须有对应资源：投影按同一不变量消费，缺了它只能在入队后炸。"""
    with pytest.raises(ValueError, match="image tokens"):
        ConversationInput(input_text="你好", display_text="你好 [[cosir-image:abc]]")


def test_presentation_accepts_image_token_bound_to_uploaded_image() -> None:
    asset = "a" * 64
    message = ConversationInput(
        input_text="描述",
        display_text=f"描述\n[[cosir-image:{asset}]]",
        image_paths=[f"attachments/{asset}.png"],
    )

    assert message.presentation().image_paths == [f"attachments/{asset}.png"]


def _input_service(monkeypatch, *, supports_reasoning_effort: bool = False) -> UserInputService:
    """不触达存储的 ``UserInputService``：Task 归属 workspace 7，模型不支持图片。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _id: SimpleNamespace(
                supports_reasoning_effort=supports_reasoning_effort, supports_image=False
            )
        ),
    )
    service = UserInputService.__new__(UserInputService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(workspace_id=7))
    return service


def test_run_service_prepares_new_command_for_model_and_persistence(monkeypatch) -> None:
    attachment_path = Path(__file__)
    service = _input_service(monkeypatch)
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

    prepared = service.prepare(7, command, 3).message

    assert prepared.input_text == f"请阅读 {attachment_path}"
    assert prepared.display_text == command.display_text
    assert prepared.attachments[0]["id"] == "readme"


def test_input_preparation_keeps_run_options_out_of_message(monkeypatch) -> None:
    service = _input_service(monkeypatch, supports_reasoning_effort=True)
    queued = service.prepare(
        7,
        ConversationRunCommand(
            display_text="检查项目",
            ban_tools=["execute_terminal"],
        ),
        3,
        "high",
    )
    assert queued.launch.options.ban_tools == ["execute_terminal"]
    assert queued.launch.options.reasoning_effort == "high"
    assert queued.launch.model_config_id == 3
    assert set(queued.message.model_dump()) == {
        "input_text",
        "display_text",
        "image_paths",
        "attachments",
    }


def test_run_service_accepts_directory_as_one_ordinary_attachment(monkeypatch) -> None:
    attachment_path = Path(__file__).parent
    service = _input_service(monkeypatch)
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

    prepared = service.prepare(7, command, 3).message

    assert prepared.input_text == f"请检查 {attachment_path}"
    assert prepared.attachments == [
        {
            "id": "references",
            "name": "references",
            "content_type": "application/x-directory",
            "path": str(attachment_path),
        }
    ]


def test_edit_preparation_reuses_target_message_attachment(monkeypatch) -> None:
    attachment_path = Path(__file__)
    previous = ConversationInput(
        input_text=f"请阅读 {attachment_path}",
        display_text="请阅读 [[cosir-file:readme]]",
        attachments=[
            {
                "id": "readme",
                "name": "README.md",
                "content_type": "text/markdown",
                "path": str(attachment_path),
            }
        ],
    )
    service = _input_service(monkeypatch, supports_reasoning_effort=True)
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
    prepared = service.prepare(7, command, 3, None, reuse=previous.presentation())
    assert prepared.message.input_text == f"请再次阅读 {attachment_path}"
    assert prepared.message.attachments[0]["path"] == str(attachment_path)


def test_prepared_text_does_not_rescan_attachment_paths(monkeypatch) -> None:
    """文件路径插入正文后不再被二次解析；顺带固定「这一层不 stat 附件」。

    路径里带 token 或 Markdown 围栏时，分两趟替换会把模型正文改坏；不存在于磁盘的路径也应
    照常绑定——存在性由真正读取附件的工具判断。
    """
    path = "/nonexistent/[[cosir-image:abc]]/report```.md"
    service = _input_service(monkeypatch)
    command = ConversationRunCommand(
        display_text="看 [[cosir-file:report]]",
        attachments=[
            ConversationRunAttachmentInput(
                id="report", name="report.md", content_type="text/markdown", path=path
            )
        ],
    )

    prepared = service.prepare(7, command, 3).message

    assert prepared.input_text == f"看 {path}"
    assert prepared.attachments[0]["path"] == path


def test_prepare_rejects_image_token_without_uploaded_asset(monkeypatch) -> None:
    """正文里的图片 token 没绑定上传图片时在入队前拒绝，不能留给展示投影抛错。"""
    service = _input_service(monkeypatch)

    with pytest.raises(UserInputRejectedError) as caught:
        service.prepare(7, ConversationRunCommand(display_text="你好 [[cosir-image:abc]]"), 3)

    assert caught.value.code == "IMAGE_ATTACHMENT_UNAVAILABLE"


def test_prepare_maps_missing_model_config_to_input_rejection(monkeypatch) -> None:
    """模型配置缺失不是「寻址不到 Task」，不能以 KeyError 冒到 404 映射。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: SimpleNamespace(get_config=lambda _id: (_ for _ in ()).throw(KeyError(_id))),
    )
    service = UserInputService.__new__(UserInputService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(workspace_id=7))

    with pytest.raises(UserInputRejectedError) as caught:
        service.prepare(7, ConversationRunCommand(display_text="你好"), 3)

    assert caught.value.code == "MODEL_CONFIG_UNAVAILABLE"


def test_run_crud_clone_preserves_options_and_resets_progress() -> None:
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
        status="failed",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        options=ConversationRunOptions(reasoning_effort="high"),
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
        assert cloned.execution_data == ReactExecutionState()
        assert cloned.options == source.options
    finally:
        engine.dispose()


def test_context_clone_copies_transport_fields_to_real_row() -> None:
    metadata = {"status": "completed", "display_data": {"kind": "read-file-meta", "path": "a.py"}}
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = ConversationTaskContextCrud.__new__(ConversationTaskContextCrud)
        store = ContextStore.__new__(ContextStore)
        store._crud = crud
        service = ConversationTaskContextService.__new__(ConversationTaskContextService)
        service._crud = crud
        service._store = store
        with Session(engine) as session:
            session.add(
                ConversationTaskContextRecord(
                    task_id=7,
                    run_id=None,
                    message={"role": "system", "content": "fixed system prompt"},
                    include_in_context=True,
                    sequence=0,
                )._to_model()
            )
            session.add(
                ConversationTaskContextModel(
                    message_id="test-message",
                    id=41,
                    task_id=7,
                    run_id=11,
                    message_json=json.dumps(
                        {"role": "tool", "tool_call_id": "call-1", "content": "source"}
                    ),
                    include_in_context=True,
                    sequence=3,
                    metadata_json=json.dumps(metadata),
                )
            )
            session.flush()

            service.clone_for_fork(7, 8, {11: 22}, session, max_sequence=3)
            cloned_row = session.scalar(
                select(ConversationTaskContextModel).where(
                    ConversationTaskContextModel.task_id == 8,
                    ConversationTaskContextModel.sequence == 1,
                )
            )

            assert cloned_row is not None
            assert cloned_row.run_id == 22
            assert cloned_row.message_id == "test-message"
            assert json.loads(cloned_row.message_json) == {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "source",
            }
            assert json.loads(cloned_row.metadata_json) == ToolMessageMetadata.model_validate(
                metadata
            ).model_dump(mode="json")
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
            message={"role": "tool", "content": "done", "tool_call_id": "call-1"},
            include_in_context=True,
            sequence=1,
            metadata=ToolMessageMetadata(status="completed"),
        )
        conflicting_sequence = ConversationTaskContextRecord(
            task_id=7,
            run_id=11,
            message={"role": "tool", "content": "other", "tool_call_id": "call-2"},
            include_in_context=True,
            sequence=1,
            metadata=ToolMessageMetadata(status="completed"),
        )
        with Session(engine) as session:
            crud.create(first, session=session)
            with pytest.raises(IntegrityError):
                crud.create(conflicting_sequence, session=session)
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
                    status="running",
                    options=ConversationRunOptions().model_dump(mode="json"),
                    execution_data=ReactExecutionState().model_dump(mode="json"),
                )
            )
            context = ConversationTaskContextRecord(
                task_id=7,
                run_id=11,
                message={
                    "role": "assistant",
                    "content": "I will inspect it",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "a.py"}),
                            },
                        }
                    ],
                },
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
        context_store = ContextStore.__new__(ContextStore)
        context_store._crud = context_crud
        context_service._crud = context_crud
        context_service._store = context_store
        service = RecoverService.__new__(RecoverService)
        service._run = run_crud
        run_state = ConversationRunLedger.__new__(ConversationRunLedger)
        run_state._run = run_crud
        service._ledger = run_state
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
                if record.message["role"] == "tool"
            ]

        assert len(recovered) == 1
        assert run_row is not None
        assert run_row.status == "cancelled"
        assert run_row.end_reason == "runtime_restarted"
        assert len(repaired) == 1
        assert repaired[0].message.get("tool_call_id") == "call-1"
        assert repaired[0].run_id == 11
        # 统一收口为取消：占位与 Run 终态同口径，冷读重建据此得到 cancelled tool part。
        assert repaired[0].metadata == ToolMessageMetadata.model_validate({"status": "cancelled"})
        # 不处理快照：恢复路径不投影任何事件。
        assert projected == []
    finally:
        engine.dispose()


def test_task_record_round_trips_current_run_without_run_owned_context_window() -> None:
    now = _timestamp()
    model = TaskModel(
        id=7,
        workspace_id=3,
        title="task",
        task_type="user",
        current_run_id=11,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    record = TaskRecord.from_model(model)

    assert record.current_run_id == 11
    assert "context_window_total" not in record.to_dict()
    assert record.to_dict()["current_run_id"] == 11
    round_tripped = record.to_model()
    assert round_tripped.current_run_id == 11
    assert "context_window_total" not in TaskModel.__table__.columns


def test_task_crud_new_task_has_no_current_run() -> None:
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
                creation_command_id=None,
                extra=None,
            )

        assert record.current_run_id is None
    finally:
        engine.dispose()


def test_fresh_app_schema_does_not_create_task_snapshots_table() -> None:
    engine = create_engine("sqlite://")
    try:
        initialize_app_schema(engine)
        assert "conversation_task_snapshots" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
