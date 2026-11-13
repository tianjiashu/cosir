import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession, sessionmaker

import app.service.user_input.user_input_service as user_input_module
from app.core.context.context_store import ContextStore
from app.core.events import RunStatusChangedEvent
from app.core.workflow.run_ledger import (
    RunLedger,
)
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.message_queue.session_message import UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.models.run_command import RunCommand
from app.models.run_options import RunOptions
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_metadata import ToolMessageMetadata
from app.models.session_extra import SessionExtra
from app.models.session_record import SessionRecord
from app.service.conversation.session_context_service import (
    SessionContextService,
)
from app.service.recover_service import RecoverService
from app.service.user_input.user_input_service import UserInputService
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.model.base import StorageBase
from app.storage.model.run_model import RunModel
from app.storage.model.session_context_model import SessionContextModel
from app.storage.model.session_model import SessionModel
from app.storage.store_engines import initialize_app_schema
from tests.model_config_fixtures import model_config_record, model_configs


def _timestamp() -> datetime:
    return datetime.now(UTC)


def test_context_record_round_trips_with_row_identity_and_metadata() -> None:
    metadata = {"status": "completed", "display_data": {"kind": "read-file-meta", "path": "a.py"}}
    record = SessionContextRecord(
        session_id=7,
        run_id=11,
        message={"role": "tool", "tool_call_id": "call-1", "content": "hello"},
        include_in_context=True,
        sequence=3,
        id=41,
        metadata=metadata,
        turn=1,
    )

    model = record._to_model()
    restored = SessionContextRecord._from_model(model)

    assert model.id == 41
    assert restored.id == 41
    assert restored.message_id == record.message_id
    assert restored.message == record.message
    assert restored.metadata == ToolMessageMetadata.model_validate(metadata)
    assert json.loads(model.metadata_json) == ToolMessageMetadata.model_validate(
        metadata
    ).model_dump(mode="json")


def test_context_storage_exposes_only_canonical_message_fields() -> None:
    assert {column.name for column in SessionContextModel.__table__.columns} == {
        "id",
        "session_id",
        "run_id",
        "message_json",
        "message_id",
        "metadata_json",
        "project_to_transport",
        "include_in_context",
        "sequence",
        "turn",
        "created_at",
        "updated_at",
    }


def test_context_record_rejects_malformed_metadata_json() -> None:
    model = SessionContextModel(
        message_id="test-message",
        session_id=7,
        run_id=11,
        message_json=json.dumps({"role": "user", **{"content": "hello"}}),
        include_in_context=True,
        sequence=3,
        metadata_json="{not-json",
    )

    with pytest.raises((TypeError, ValueError)):
        SessionContextRecord._from_model(model)


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
    record = RunRecord(
        id=11,
        session_id=7,
        status="failed",
        created_at=_timestamp(),
        updated_at=_timestamp(),
        usage=usage,
        error=error,
    )

    model = record.to_model()
    restored = RunRecord.from_model(model)

    assert json.loads(model.usage_json) == usage
    assert json.loads(model.error_json) == error
    assert restored.usage == usage
    assert restored.error == error


def test_user_input_body_rejects_legacy_assistant_input_wrapper() -> None:
    with pytest.raises((TypeError, ValueError)):
        UserInputBody.model_validate(
            {
                "assistant_input": {
                    "version": 1,
                    "text": '请阅读 <file path="/a/readme.md" />',
                }
            }
        )


def test_presentation_is_a_plain_binding() -> None:
    """展示绑定只承载事实，不在模型层重复校验：形状由 ``UserInputService`` 保证。"""
    asset = "a" * 64
    presentation = UserInputPresentation(
        text='描述 <ref path="src/a.py" lines="1-2" />',
        image_paths=[f"attachments/{asset}.png"],
    )

    assert presentation.model_dump() == {
        "text": '描述 <ref path="src/a.py" lines="1-2" />',
        "image_paths": [f"attachments/{asset}.png"],
    }


def _input_service(
    monkeypatch, workspace_root: Path, *, supports_reasoning_effort: bool = False
) -> UserInputService:
    """不触达存储的 ``UserInputService``：Session 归属 workspace 7，模型不支持图片。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: model_configs(
            lambda _id: model_config_record(
                model_name="m",
                context_window_k=8,
                supports_reasoning_effort=supports_reasoning_effort,
            )
        ),
    )
    monkeypatch.setattr(
        user_input_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(workspace_root))
        ),
    )
    service = UserInputService.__new__(UserInputService)
    service._session = SimpleNamespace(get=lambda _session_id: SimpleNamespace(workspace_id=7))
    return service


def test_run_service_prepares_new_command_for_model_and_persistence(monkeypatch, tmp_path) -> None:
    attachment_path = tmp_path / "README.md"
    attachment_path.write_text("hello", encoding="utf-8")
    service = _input_service(monkeypatch, tmp_path)
    command = RunCommand(text=f'请阅读 <file path="{attachment_path}" />')

    prepared = service.prepare(7, command, "test/m3", None, "main_agent")

    # 模型正文与展示正文是同一份规范化胶囊文本。
    assert prepared.text == command.text
    assert prepared.presentation.text == command.text
    assert prepared.presentation.image_paths == []


def test_prepare_returns_a_user_input_body_for_the_caller_to_queue(monkeypatch, tmp_path) -> None:
    """投递方式是信封属性，由入队方按 Session 当时的阶段决定；这里只产出消息体。"""
    service = _input_service(monkeypatch, tmp_path)

    body = service.prepare(7, RunCommand(text="检查项目"), "test/m3", None, "main_agent")

    assert isinstance(body, UserInputBody)
    assert body.text == "检查项目"
    assert body.launch.agent_id == "main_agent"


def test_input_preparation_keeps_run_options_out_of_message(monkeypatch, tmp_path) -> None:
    service = _input_service(monkeypatch, tmp_path, supports_reasoning_effort=True)
    queued = service.prepare(
        7,
        RunCommand(text="检查项目", disabled_tool_groups=["终端工具"]),
        "test/m3",
        "high",
        "main_agent",
    )
    assert queued.launch.options.disabled_tool_groups == ["终端工具"]
    assert queued.launch.options.reasoning_effort == "high"
    assert queued.launch.model_config_id == "test/m3"
    # Run 的配置只随 ``launch`` 走；消息体自身没有散落的执行选项或附件字段。
    assert set(queued.model_dump()) == {"text", "presentation", "launch"}


def test_prepare_maps_missing_model_config_to_input_rejection(monkeypatch, tmp_path) -> None:
    """模型配置缺失不是「寻址不到 Session」，不能以 KeyError 冒到 404 映射。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: model_configs(lambda _id: (_ for _ in ()).throw(KeyError(_id))),
    )
    monkeypatch.setattr(
        user_input_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(tmp_path))
        ),
    )
    service = UserInputService.__new__(UserInputService)
    service._session = SimpleNamespace(get=lambda _session_id: SimpleNamespace(workspace_id=7))

    with pytest.raises(UserInputRejectedError) as caught:
        service.prepare(7, RunCommand(text="你好"), "test/m3", None, "main_agent")

    assert caught.value.code == "MODEL_CONFIG_UNAVAILABLE"


def test_context_crud_raises_on_sequence_collision() -> None:
    """序号冲突（调用方分配序号出错）必须原样抛出，不得在持久化层被吞掉。

    工具调用身份不再是数据库事实（``tool_call_id`` 列已删除），因此 ``create`` 的唯一冲突来源
    只有 ``(session_id, sequence)``；它代表序号 owner 出错，只能暴露。
    """

    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = SessionContextCrud.__new__(SessionContextCrud)
        first = SessionContextRecord(
            session_id=7,
            run_id=11,
            message={"role": "tool", "content": "done", "tool_call_id": "call-1"},
            include_in_context=True,
            sequence=1,
            metadata=ToolMessageMetadata(status="completed"),
            turn=1,
        )
        conflicting_sequence = SessionContextRecord(
            session_id=7,
            run_id=11,
            message={"role": "tool", "content": "other", "tool_call_id": "call-2"},
            include_in_context=True,
            sequence=1,
            metadata=ToolMessageMetadata(status="completed"),
            turn=1,
        )
        with DbSession(engine) as db:
            crud.create(first, db=db)
            with pytest.raises(IntegrityError):
                crud.create(conflicting_sequence, db=db)
    finally:
        engine.dispose()


def test_real_orphan_recovery_persists_run_and_interrupted_tool_repair(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with DbSession(engine) as db:
            db.add(SessionModel(id=7, workspace_id=3, title="session", creation_command_id=None))
            db.add(
                RunModel(
                    id=11,
                    session_id=7,
                    status="running",
                    options=RunOptions().model_dump(mode="json"),
                    execution_data=ReactExecutionState().model_dump(mode="json"),
                )
            )
            context = SessionContextRecord(
                session_id=7,
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
                turn=1,
            )
            db.add(context._to_model())
            db.commit()

        run_crud = RunCrud.__new__(RunCrud)
        run_crud._db_factory = factory
        context_service = SessionContextService.__new__(SessionContextService)
        context_crud = SessionContextCrud.__new__(SessionContextCrud)
        context_crud._db_factory = factory
        context_store = ContextStore.__new__(ContextStore)
        context_store._crud = context_crud
        context_service._crud = context_crud
        context_service._store = context_store
        service = RecoverService.__new__(RecoverService)
        service._run = run_crud
        run_state = RunLedger.__new__(RunLedger)
        run_state._run = run_crud
        service._ledger = run_state
        service._context = context_service
        service._db_factory = factory
        projected: list[RunStatusChangedEvent] = []

        class _Projector:
            def process(self, event: RunStatusChangedEvent) -> None:
                projected.append(event)

        monkeypatch.setattr(
            "app.service.depends.get_conversation_event_projector", lambda: _Projector()
        )

        recovered = service.recover_orphaned_runs()

        with DbSession(engine) as db:
            run_row = db.get(RunModel, 11)
            context_rows = db.scalars(
                select(SessionContextModel).where(
                    SessionContextModel.session_id == 7,
                    SessionContextModel.run_id == 11,
                )
            ).all()
            repaired = [
                record
                for record in (
                    SessionContextRecord._from_model(row) for row in context_rows
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


def test_session_record_round_trips_current_run_without_run_owned_context_window() -> None:
    now = _timestamp()
    model = SessionModel(
        id=7,
        workspace_id=3,
        title="session",
        session_type="user",
        current_run_id=11,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    record = SessionRecord.from_model(model)

    assert record.current_run_id == 11
    assert "context_window_total" not in record.to_dict()
    assert record.to_dict()["current_run_id"] == 11
    round_tripped = record.to_model()
    assert round_tripped.current_run_id == 11
    assert "context_window_total" not in SessionModel.__table__.columns


def test_session_crud_new_session_has_no_current_run() -> None:
    engine = create_engine("sqlite://")
    StorageBase.metadata.create_all(engine)
    try:
        crud = SessionCrud.__new__(SessionCrud)
        with DbSession(engine) as db:
            record = crud._build_and_flush(
                db,
                workspace_id=3,
                title="empty",
                session_type="user",
                parent_session_id=None,
                creation_command_id=None,
                extra=SessionExtra(),
            )

        assert record.current_run_id is None
    finally:
        engine.dispose()


def test_fresh_app_schema_does_not_create_session_snapshots_table() -> None:
    engine = create_engine("sqlite://")
    try:
        initialize_app_schema(engine)
        assert "conversation_session_snapshots" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
