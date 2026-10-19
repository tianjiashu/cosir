"""Task 3 canonical message/tool/run write-path tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import (
    ChatCompletionToolMessageParam,
)

import app.core.workflow.node_helper.tool_call_lifecycle as lifecycle_module
from app.assistant_transport.event import RunStatusChangedEvent
from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.workflow.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.node_helper import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_failure import run_failure_message
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
from tests.sdk_fixtures import sdk_completion
from tests.test_task5_acceptance import canonical_store as canonical_store


class _RecordingContextService:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self.current_max_sequence = 0

    def append(self, *args: Any, **kwargs: Any) -> None:
        self.appended.append({"args": args, "kwargs": kwargs})
        self.current_max_sequence = args[3]
        return ContextEntry(args[2], args[1], args[3])

    def entries_in_context(self, _task_id: int) -> list[ContextEntry]:
        return []

    def max_sequence(self, _task_id: int) -> int:
        return self.current_max_sequence

    def delete_by_run_id(self, _task_id: int, _run_id: int) -> None:
        return None


def _runtime_manager(service: _RecordingContextService) -> RuntimeMessageContextManager:
    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace_root = ""
    manager.context_service = service
    manager.current_run_id = 11
    manager._system_entry = None
    manager._entries = []
    manager._message_sequence = 1
    manager._system_entry = ContextEntry({"role": "system", "content": "system"}, None, -1)
    return manager


def test_message_context_persists_complete_ai_fields_and_ordered_parts() -> None:
    service = _RecordingContextService()
    manager = _runtime_manager(service)
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "think",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }
        ],
    }

    manager.add_message(message)

    persisted = service.appended[0]
    assert persisted["args"][2] == message


def test_begin_run_binds_identity_without_rewriting_context() -> None:
    service = _RecordingContextService()
    manager = _runtime_manager(service)
    message = {"role": "user", "content": "existing"}
    manager._entries = [ContextEntry(message, 11, 1)]
    manager.begin_run(SimpleNamespace(id=12, task_id=7))
    assert manager.current_run_id == 12
    assert manager._entries[0].message is message
    assert service.appended == []


def test_sdk_response_projection_preserves_raw_tool_arguments():
    from app.core.llm_provider.message_codec import assistant_message

    response = sdk_completion(
        "answer",
        reasoning_content="think",
        tool_calls=[
            {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{"}}
        ],
    )
    projected = assistant_message(response.choices[0].message)
    assert projected["content"] == "answer"
    assert projected["reasoning_content"] == "think"
    assert projected["tool_calls"][0]["function"]["arguments"] == "{"


def test_tool_call_lifecycle_freezes_presentation_in_serializable_state(monkeypatch) -> None:
    # 展示声明只在 ``allowed`` 桶写入，故本用例必须把工具放进本轮允许集合。
    manager = ToolCallLifecycleManager(allows_tools=("read_file",))
    tool_definitions = [
        SimpleNamespace(name="read_file", display=SimpleNamespace(to_dict=lambda: {"verb": "Read"}))
    ]
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            model_tools=tool_definitions,
            # 与真实契约同形：``_valid_tool_name`` / ``_presentation_for`` 读 ``all_vaild_tools``。
            all_vaild_tools=tool_definitions,
        )
    )
    events: list[Any] = []
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    manager = manager.create(
        task_id=7,
        run_id=11,
        step_id="step-1",
        raw_tool_calls=[
            {"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": ""}}
        ],
    )

    assert manager.valid_calls["call-1"].presentation == {"verb": "Read"}


def _tool_summary() -> dict[str, Any]:
    return {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "content": "file body",
        "error": "",
        "reason": "",
        "retryable": False,
        "display_data": {"kind": "read-file-meta", "path": "a.py"},
        "artifact_data": {"before": "secret"},
    }


def test_tool_settle_persists_before_transport_event_and_is_idempotent(monkeypatch) -> None:
    order: list[str] = []

    class _RuntimeContext:
        def add_message(self, message: ChatCompletionToolMessageParam, **kwargs: Any) -> str:
            order.append("database")
            assert "status" not in message
            assert kwargs["metadata"].display_data == {
                "kind": "read-file-meta",
                "path": "a.py",
            }
            assert "artifact_data" not in kwargs["metadata"].model_dump()
            return "appended"

    writer = order.append
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: {
                "role": "tool",
                "content": observation.content,
                "tool_call_id": observation.tool_call_id,
            },
            model_tools=[],
        )
    )
    runtime_context = _RuntimeContext()
    manager = ToolCallLifecycleManager(
        valid_calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )

    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_message_context", lambda: runtime_context)
    monkeypatch.setattr(
        lifecycle_module, "get_stream_writer", lambda: lambda event: writer("event")
    )

    first = manager.settle_batch(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summaries=[_tool_summary()],
        inherited_error_count=0,
    )
    second = first.lifecycle.settle_batch(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summaries=[_tool_summary()],
        inherited_error_count=0,
    )

    assert order == ["database", "event"]
    assert second.lifecycle.valid_calls["call-1"].status == "completed"


def test_failed_tool_event_uses_sanitized_display_data(monkeypatch) -> None:
    """失败终态的展示数据与短提示取自 ``display_data`` 分类，不携带原始 provider 文本。"""

    events: list[Any] = []

    class _RuntimeContext:
        def add_message(self, message: ChatCompletionToolMessageParam, **kwargs: Any) -> str:
            assert kwargs["metadata"].display_data == {
                "kind": "read-file-meta",
                "path": "a.py",
            }
            assert kwargs["metadata"].error == "执行失败"
            return "appended"

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: {
                "role": "tool",
                "content": observation.content,
                "tool_call_id": observation.tool_call_id,
            },
            model_tools=[],
        )
    )
    manager = ToolCallLifecycleManager(
        valid_calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_message_context", lambda: _RuntimeContext())
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    updated, status = manager.settle(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summary={**_tool_summary(), "status": "error", "error": "raw provider detail"},
    )

    assert status == "failed"
    assert updated.valid_calls["call-1"].status == "failed"
    assert events[0].display_data == {"kind": "read-file-meta", "path": "a.py"}
    assert events[0].error == "执行失败"


def test_tool_settle_overwrites_existing_result_row_and_still_emits_terminal_event(
    monkeypatch,
) -> None:
    """命中既有结果行（占位）时结算覆盖该行，并照常发终态事件。"""

    events: list[Any] = []
    writes: list[str] = []

    class _RuntimeContext:
        def add_message(self, _message: ChatCompletionToolMessageParam, **_kwargs: Any) -> str:
            writes.append("replaced")
            return "replaced"

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: {
                "role": "tool",
                "content": observation.content,
                "tool_call_id": observation.tool_call_id,
            },
            model_tools=[],
        )
    )
    manager = ToolCallLifecycleManager(
        valid_calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_message_context", lambda: _RuntimeContext())
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    manager.settle(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summary=_tool_summary(),
    )

    assert writes == ["replaced"]
    assert [event.status for event in events] == ["completed"]


def test_orphan_recovery_commits_run_and_tool_closure_before_return() -> None:
    """恢复：Run 终态与工具收口在同一个事务内提交，提交后不再投影快照。"""

    order: list[str] = []
    recovered_run = SimpleNamespace(
        id=11, task_id=7, status="cancelled", execution_data=ConversationRunProgress()
    )

    class _Transaction:
        def __enter__(self):
            order.append("begin")
            return object()

        def __exit__(self, *_args: object) -> None:
            order.append("commit")

    class _SessionFactory:
        def begin(self):
            return _Transaction()

    class _RunCrud:
        def list_recoverable(self):
            return [recovered_run]

        def update_status_if_in(
            self,
            run_id: int,
            target_status: str,
            allowed_statuses: tuple[str, ...],
            end_reason: str | None = None,
            final_output: str | None = None,
            usage: object = None,
            error: object = None,
            session: object = None,
            execution_data: object = None,
        ) -> object:
            assert session is not None
            order.append("run")
            return recovered_run

    class _ContextService:
        def invalidate_waiting_calls(self, task_id, run_id, session):
            pass

        def close_unclosed_tool_calls_for_run(
            self, task_id: int, run_id: int, session: object = None
        ) -> list[str]:
            assert (task_id, run_id) == (7, 11)
            assert session is not None
            order.append("context")
            return ["call-1"]

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _RunCrud()
    service._context = _ContextService()
    service._session_factory = _SessionFactory()

    assert service.recover_orphaned_runs() == [recovered_run]
    assert order == ["begin", "run", "context", "commit"]


def _creation_service(store, monkeypatch, projector):
    """创建与冷读共用临时 SQLite，模型解析只注入配置事实。"""
    from app.storage.model.model_config_model import ModelConfigModel

    with store.factory.begin() as session:
        session.add(
            ModelConfigModel(
                id=1,
                config_name="test",
                base_url="http://localhost",
                api_key="test",
                model_name="test",
                context_window_k=128,
            )
        )
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = store.runs
    service._task = store.tasks
    service._context = store.context
    service._session_factory = store.factory
    monkeypatch.setattr("app.service.depends.get_conversation_event_projector", lambda: projector)
    monkeypatch.setattr(
        "app.service.conversation_run.conversation_run_service.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _id: SimpleNamespace(
                supports_reasoning_effort=False,
                supports_image=False,
                context_window_k=128,
            )
        ),
    )
    monkeypatch.setattr(
        "app.service.conversation_run.conversation_input_preparation_service.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _id: SimpleNamespace(
                supports_reasoning_effort=False,
                supports_image=False,
                context_window_k=128,
            )
        ),
    )
    return service


def test_create_run_commits_inbox_and_current_run_before_projector(request, monkeypatch):
    """独立连接在事件发布时已能看到整组事实，context 留待模型边界消费。"""
    from app.service.conversation_run.task_input_service import TaskInputService

    store = request.getfixturevalue("canonical_store")
    events = []

    class Projector:
        def process(self, event):
            assert store.tasks.get(store.task.id).current_run_id == event.run_id
            assert store.runs.get(event.run_id).context_window_total == 128000
            assert (
                TaskInputService().pending(store.task.id)[0].payload.message.input_text == "hello"
            )
            assert store.contexts.get(store.task.id, include_in_context=False) == []
            events.append(event)

    service = _creation_service(store, monkeypatch, Projector())
    run = service.create_run(
        store.task.id,
        model_config_id=1,
        run_command=ConversationRunCommand(display_text="hello"),
    )
    assert len(events) == 1
    assert events[0].run_id == run.id


def test_create_run_with_external_session_defers_initialization_events_to_owner(
    request,
    monkeypatch,
):
    """外部事务内的 Run/inbox/指针不可提前暴露，回滚不留下任何一项。"""
    from app.service.conversation_run.task_input_service import TaskInputService
    from app.storage.crud.task_input_crud import TaskInputCrud

    store = request.getfixturevalue("canonical_store")
    events = []
    service = _creation_service(store, monkeypatch, SimpleNamespace(process=events.append))
    with store.factory() as session:
        service.create_run(
            store.task.id,
            model_config_id=1,
            run_command=ConversationRunCommand(display_text="hello"),
            session=session,
        )
        assert (
            TaskInputCrud().list_pending(session, store.task.id)[0].payload["message"]["input_text"]
            == "hello"
        )
        assert store.runs.list_by_task(store.task.id) == []
        assert store.tasks.get(store.task.id).current_run_id is None
        assert TaskInputService().pending(store.task.id) == []
        assert events == []
        session.rollback()
    assert store.runs.list_by_task(store.task.id) == []
    assert TaskInputService().pending(store.task.id) == []
    assert store.tasks.get(store.task.id).current_run_id is None
    assert events == []


def _run_record() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=11,
        task_id=7,
        status="running",
        created_at=now,
        updated_at=now,
    )


def test_failed_run_persists_usage_and_controlled_error_before_projector() -> None:
    order: list[str] = []
    run = _run_record()

    class _RunCrud:
        def update_status_if_in(self, *args: Any, **kwargs: Any):
            order.append("database")
            assert kwargs["usage"] == {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "cache_hit_tokens": 1,
                "cache_miss_tokens": 2,
                "reasoning_tokens": None,
            }
            assert kwargs["error"] == {
                "code": "tool_error_limit_reached",
                "message": run_failure_message("tool_error_limit_reached"),
            }
            run.status = "failed"
            return run

        def get(self, _run_id: int):
            return run

    class _Projector:
        def process(self, event: RunStatusChangedEvent, **kwargs: Any) -> None:
            order.append("event")
            assert event.status is ConversationRunStatus.FAILED

    service = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run = _RunCrud()
    service._session_factory = None
    import app.service.depends as depends

    depends_getter = depends.get_conversation_event_projector
    depends.get_conversation_event_projector = lambda: _Projector()
    try:
        result = service.fail_run_if_running(
            11,
            end_reason="tool_error_limit_reached",
            usage_stats=ConversationRunUsageStats(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cache_hit_tokens=1,
                cache_miss_tokens=2,
            ),
        )
    finally:
        depends.get_conversation_event_projector = depends_getter

    assert result is run
    assert order == ["database", "event"]


def test_terminal_run_projector_failure_is_non_fatal_after_database_commit() -> None:
    run = _run_record()
    committed: list[str] = []

    class _RunCrud:
        def update_status_if_in(self, *args: Any, **kwargs: Any):
            committed.append("database")
            run.status = "completed"
            return run

        def get(self, _run_id: int):
            return run

    class _Projector:
        def process(self, _event: Any, **_kwargs: Any) -> None:
            raise RuntimeError("transport unavailable")

    service = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run = _RunCrud()
    import app.service.depends as depends

    original_getter = depends.get_conversation_event_projector
    depends.get_conversation_event_projector = lambda: _Projector()
    try:
        # 终态落库先于投影：投影失败向上传播，但已提交的 Run 事实完好。
        with pytest.raises(RuntimeError, match="transport unavailable"):
            service.complete_run_if_running(11, final_output="done")
    finally:
        depends.get_conversation_event_projector = original_getter

    assert committed == ["database"]
