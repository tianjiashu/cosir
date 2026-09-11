"""Task 3 canonical message/tool/run write-path tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.assistant_transport.event import RunStatusChangedEvent
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.nodes.helper import tool_call_lifecycle as lifecycle_module
from app.core.workflows.nodes.helper.model_chunk import ModelChunkProcessor
from app.core.workflows.nodes.helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_task_context_service import ConversationTaskContextService


class _RecordingContextService:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self.current_max_sequence = 0

    def append(self, *args: Any, **kwargs: Any) -> None:
        self.appended.append({"args": args, "kwargs": kwargs})
        self.current_max_sequence = args[3]

    def entries_in_context(self, _task_id: int) -> list[ContextEntry]:
        return []

    def max_sequence(self, _task_id: int) -> int:
        return self.current_max_sequence

    def delete_by_run_id(self, _task_id: int, _run_id: int) -> None:
        return None


def _runtime_manager(service: _RecordingContextService) -> RuntimeContextManager:
    manager = object.__new__(RuntimeContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace_root = ""
    manager.total_tokens = 0
    manager.used_tokens = 0
    manager.compressor = None
    manager.context_service = service
    manager.current_run_id = 11
    manager.is_fork = False
    manager._system_entry = None
    manager._entries = []
    manager._message_sequence = 1
    manager._listeners = []
    manager._tool_schemas = ()
    manager._system_entry = ContextEntry(SystemMessage(content="system"), None, -1)
    return manager


def test_runtime_context_persists_complete_ai_fields_and_ordered_parts() -> None:
    service = _RecordingContextService()
    manager = _runtime_manager(service)
    message = AIMessage(
        content="answer",
        additional_kwargs={"reasoning_content": "think", "provider_flag": True},
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
        invalid_tool_calls=[],
        response_metadata={"model_name": "model-x"},
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        id="msg-1",
    )

    manager.add_message(
        message,
        transport_parts=[
            {"type": "text", "text": "answer", "status": "completed"},
            {"type": "reasoning", "text": "think", "status": "completed"},
            {
                "type": "tool-call",
                "toolCallId": "call-1",
                "toolName": "read_file",
                "args": {"path": "a.py"},
                "presentation": {"verb": "Read"},
                "status": "pending",
                "isError": False,
            },
        ],
    )

    persisted = service.appended[0]
    assert persisted["args"][2] == message
    assert persisted["kwargs"]["transport_parts"][1]["type"] == "reasoning"
    assert persisted["kwargs"]["transport_parts"][2]["presentation"] == {"verb": "Read"}


class _UserCrud:
    def __init__(self) -> None:
        self.records: list[ConversationTaskContextRecord] = []

    def get(self, _task_id: int, include_in_context: bool = True, session: Any = None):
        if include_in_context:
            return [record for record in self.records if record.include_in_context]
        return list(self.records)

    def max_sequence(self, _task_id: int) -> int:
        return max((record.sequence for record in self.records), default=0)

    def create(self, record: ConversationTaskContextRecord, session: Any = None) -> None:
        self.records.append(record)


def test_context_service_initial_user_write_is_idempotent() -> None:
    service = ConversationTaskContextService.__new__(ConversationTaskContextService)
    service._crud = _UserCrud()

    first = service.append_user_message_once(7, 11, "hello")
    second = service.append_user_message_once(7, 11, "hello")

    assert first is True
    assert second is False
    assert len(service._crud.records) == 1
    assert isinstance(service._crud.records[0].message, HumanMessage)
    assert service._crud.records[0].message.content == "hello"


def test_context_service_persists_system_prompt_once() -> None:
    service = ConversationTaskContextService.__new__(ConversationTaskContextService)
    service._crud = _UserCrud()
    prompt = SystemMessage(content="system prompt")

    assert service.ensure_system_message(7, prompt) is True
    assert service.ensure_system_message(7, prompt) is False
    assert len(service._crud.records) == 1
    assert service._crud.records[0].run_id is None
    assert service._crud.records[0].message == prompt


def test_runtime_context_fresh_run_reloads_canonical_user_after_reset(monkeypatch) -> None:
    user = ContextEntry(HumanMessage(content="hello"), 11, 1)

    class _FreshContextService:
        def __init__(self) -> None:
            self.entries = [user]
            self.reset_calls = 0
            self.user_append_calls = 0

        def delete_by_run_id(self, _task_id: int, _run_id: int) -> None:
            self.entries = []

        def reset_run_for_fresh(self, _task_id: int, _run_id: int) -> None:
            self.reset_calls += 1
            self.entries = [
                entry for entry in self.entries if isinstance(entry.message, HumanMessage)
            ]

        def append_user_message_once(
            self, _task_id: int, run_id: int, text: str, **kwargs: Any
        ) -> bool:
            self.user_append_calls += 1
            if any(
                entry.run_id == run_id and isinstance(entry.message, HumanMessage)
                for entry in self.entries
            ):
                return False
            self.entries.append(ContextEntry(HumanMessage(content=text), run_id, 1))
            return True

        def entries_in_context(self, _task_id: int) -> list[ContextEntry]:
            return list(self.entries)

        def max_sequence(self, _task_id: int) -> int:
            return max((entry.sequence for entry in self.entries), default=0)

    service = _FreshContextService()
    manager = _runtime_manager(service)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 100,
    )

    manager.begin_run(
        SimpleNamespace(id=11, task_id=7, model_name="model-x", input_text="hello"),
        "fresh",
    )

    assert [entry.message for entry in service.entries] == [user.message]
    assert [entry.message for entry in manager._entries] == [user.message]
    assert service.reset_calls == 1
    assert service.user_append_calls == 0
    assert service.entries[0] is user


def test_model_processor_collect_preserves_complete_langchain_message_semantics() -> None:
    processor = ModelChunkProcessor("reasoning_content")
    chunk = AIMessageChunk(
        content=[{"type": "text", "text": "structured", "index": 0}],
        name="assistant",
        id="msg-1",
        additional_kwargs={"provider_flag": True, "reasoning_content": "think"},
        response_metadata={"model_name": "model-x"},
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
        invalid_tool_calls=[{"name": "broken", "args": "{", "id": "call-bad", "error": "bad"}],
        tool_call_chunks=[
            {"name": "read_file", "args": '{"path":"a.py"}', "id": "call-1", "index": 0}
        ],
    )

    collected = processor.collect([chunk])

    assert isinstance(collected, AIMessage)
    assert collected.content == chunk.content
    assert collected.name == chunk.name
    assert collected.id == chunk.id
    assert collected.additional_kwargs == chunk.additional_kwargs
    assert collected.response_metadata == chunk.response_metadata
    assert collected.usage_metadata == chunk.usage_metadata
    assert collected.tool_calls == chunk.tool_calls
    assert collected.invalid_tool_calls == chunk.invalid_tool_calls


def test_model_processor_builds_ordered_persisted_parts_with_frozen_presentation() -> None:
    processor = ModelChunkProcessor("reasoning_content")
    chunks = [
        AIMessageChunk(content="answer"),
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "think"}),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "read_file", "args": '{"path":"a.py"}', "id": "call-1", "index": 0}
            ],
        ),
    ]
    message = AIMessage(
        content="answer",
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
    )

    parts = processor.build_transport_parts(
        chunks,
        message,
        {"call-1": {"verb": "Read"}},
    )

    assert [part["type"] for part in parts] == ["text", "reasoning", "tool-call"]
    assert parts[2]["presentation"] == {"verb": "Read"}
    assert parts[2]["args"] == {"path": "a.py"}


def test_model_processor_keeps_tool_call_at_its_original_stream_position() -> None:
    processor = ModelChunkProcessor("reasoning_content")
    chunks = [
        AIMessageChunk(content="before "),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "read_file", "args": '{"path":"a.py"}', "id": "call-1", "index": 0}
            ],
        ),
        AIMessageChunk(content="after"),
    ]
    message = AIMessage(
        content="before after",
        tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
    )

    parts = processor.build_transport_parts(chunks, message, {"call-1": {"verb": "Read"}})

    assert [part["type"] for part in parts] == ["text", "tool-call", "text"]
    assert parts[1]["toolCallId"] == "call-1"
    assert parts[1]["args"] == {"path": "a.py"}


def test_tool_call_lifecycle_freezes_presentation_in_serializable_state(monkeypatch) -> None:
    manager = ToolCallLifecycleManager()
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            model_tools=[
                SimpleNamespace(
                    name="read_file", display=SimpleNamespace(to_dict=lambda: {"verb": "Read"})
                )
            ]
        )
    )
    events: list[Any] = []
    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    manager = manager.create(
        task_id=7,
        run_id=11,
        step_id="step-1",
        raw_tool_calls=[{"id": "call-1", "name": "read_file"}],
    )

    assert manager.calls["call-1"].presentation == {"verb": "Read"}


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
        def add_message(self, message: ToolMessage, **kwargs: Any) -> None:
            order.append("database")
            assert message.status == "success"
            assert kwargs["tool_result"]["display_data"] == {
                "kind": "read-file-meta",
                "path": "a.py",
            }
            assert "artifact_data" not in kwargs["tool_result"]

    writer = order.append
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content,
                tool_call_id=observation.tool_call_id,
                status=observation.status,
            ),
            model_tools=[],
        )
    )
    runtime_context = _RuntimeContext()
    manager = ToolCallLifecycleManager(
        calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_context)
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
    assert second.lifecycle.calls["call-1"].status == "completed"


def test_failed_tool_event_uses_sanitized_display_data_and_writer_failure_is_non_fatal(
    monkeypatch,
) -> None:
    events: list[Any] = []

    class _RuntimeContext:
        def add_message(self, message: ToolMessage, **kwargs: Any) -> None:
            assert kwargs["tool_result"]["display_data"] == {"status_hint": "执行失败"}

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content,
                tool_call_id=observation.tool_call_id,
                status=observation.status,
            ),
            model_tools=[],
        )
    )
    manager = ToolCallLifecycleManager(
        calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )
    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: _RuntimeContext())

    def failing_writer(_event: Any) -> None:
        events.append(_event)
        raise RuntimeError("stream disconnected")

    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: failing_writer)

    updated, status = manager.settle(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summary={**_tool_summary(), "status": "error", "error": "raw provider detail"},
    )

    assert status == "failed"
    assert updated.calls["call-1"].status == "failed"
    assert events[0].display_data == {"status_hint": "执行失败"}


def test_tool_settle_does_not_emit_terminal_event_when_canonical_append_is_duplicate(
    monkeypatch,
) -> None:
    events: list[Any] = []

    class _RuntimeContext:
        def add_message(self, _message: ToolMessage, **_kwargs: Any) -> bool:
            return False

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content,
                tool_call_id=observation.tool_call_id,
                status=observation.status,
            ),
            model_tools=[],
        )
    )
    manager = ToolCallLifecycleManager(
        calls={
            "call-1": ToolCallLifecycleRecord(
                tool_call_id="call-1", tool_name="read_file", status="running"
            )
        }
    )
    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: _RuntimeContext())
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    manager.settle(
        task_id=7,
        run_id=11,
        step_id="step-1",
        summary=_tool_summary(),
    )

    assert events == []


def test_runtime_context_post_commit_listener_failure_does_not_hide_durable_write() -> None:
    service = _RecordingContextService()
    manager = _runtime_manager(service)

    class _FailingListener:
        main_agent_only = False
        order = 0

        def listen(self, *_args: Any) -> None:
            raise RuntimeError("projector unavailable")

    manager.add_change_listener(_FailingListener())

    manager.add_message(AIMessage(content="durable"))

    assert len(service.appended) == 1
    assert manager._entries[-1].message.content == "durable"


def test_orphan_recovery_commits_run_and_context_repair_before_projector(monkeypatch) -> None:
    order: list[str] = []
    recovered_run = SimpleNamespace(id=11, task_id=7, status="cancelled")

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

        def cancel_recoverable_for_restart(self, run_id: int, end_reason: str, **kwargs: Any):
            assert kwargs["session"] is not None
            order.append("run")
            return recovered_run

    class _ContextService:
        def recover_interrupted_run(self, task_id: int, run_id: int, **kwargs: Any) -> list[str]:
            assert (task_id, run_id) == (7, 11)
            assert kwargs["session"] is not None
            order.append("context")
            return ["call-1"]

    class _Projector:
        def process(self, _event: Any) -> None:
            order.append("projector")
            assert order == ["begin", "run", "context", "commit", "projector"]

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _RunCrud()
    service._context = _ContextService()
    service._session_factory = _SessionFactory()
    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector", lambda: _Projector()
    )

    assert service.recover_orphaned_runs() == [recovered_run]
    assert order == ["begin", "run", "context", "commit", "projector"]


def test_create_run_writes_user_context_and_task_current_run_before_projector(monkeypatch) -> None:
    order: list[str] = []
    run = SimpleNamespace(id=11, task_id=7, input_text="hello")

    class _RunCrud:
        def create(self, *args: Any, **kwargs: Any):
            order.append("run")
            return run

    class _TaskCrud:
        def set_current_run_id(self, task_id: int, run_id: int, **kwargs: Any) -> None:
            order.append("task")
            assert (task_id, run_id) == (7, 11)

    class _ContextService:
        def append_user_message_once(
            self, task_id: int, run_id: int, text: str, **kwargs: Any
        ) -> bool:
            order.append("user")
            assert (task_id, run_id, text) == (7, 11, "hello")
            return True

    class _Projector:
        def process(self, event: Any, **kwargs: Any) -> None:
            order.append("event")

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _RunCrud()
    service._task = _TaskCrud()
    service._context = _ContextService()
    service._session_factory = None
    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector", lambda: _Projector()
    )

    result = service.create_run(7, "hello")

    assert result is run
    assert order == ["run", "user", "task", "event", "event"]


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
                "reasoning_tokens": 0,
            }
            assert kwargs["error"] == {
                "code": "tool_error_limit_reached",
                "message": "运行失败",
                "retryable": False,
            }
            run.status = "failed"
            return run

        def get(self, _run_id: int):
            return run

    class _Projector:
        def process(self, event: RunStatusChangedEvent, **kwargs: Any) -> None:
            order.append("event")
            assert event.status is ConversationRunStatus.FAILED

    service = ConversationRunService.__new__(ConversationRunService)
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


def test_context_usage_persists_window_before_publishing_event(monkeypatch) -> None:
    order: list[str] = []

    class _TaskService:
        def update_context_usage(self, task_id: int, used: int, total: int) -> None:
            order.append("database")
            assert task_id == 7
            assert used >= 0
            assert total == 100

    class _Projector:
        def process(self, event: Any) -> None:
            order.append("event")
            assert event.type == "context_usage_updated"

    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_task_service",
        lambda: _TaskService(),
    )
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_conversation_event_projector",
        lambda: _Projector(),
    )
    listener = ContextUsageComputeListener(7, 11)
    listener.event_projector = _Projector()
    result = ListenerResult(0)
    listener.listen(
        ListenerEvent(
            ContextEventType.ADD_MESSAGE,
            [ContextEntry(HumanMessage(content="hello"), 11, 1)],
            0,
            100,
            (),
        ),
        result,
    )

    assert order == ["database", "event"]
