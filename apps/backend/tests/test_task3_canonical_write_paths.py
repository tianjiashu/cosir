"""Task 3 canonical message/tool/run write-path tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.events import RunStatusChangedEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.conversation_run_failure import run_failure_message
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.core.conversation_run_ledger import ConversationRunLedger
from app.service.recover_service import RecoverService
from tests.sdk_fixtures import sdk_completion


class _RecordingContextStore:
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


def _runtime_manager(context_store: _RecordingContextStore) -> RuntimeMessageContextManager:
    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace = SimpleNamespace(id=7, root_path="")
    manager.context_store = context_store
    manager.current_run_id = 11
    manager._system_entry = None
    manager._entries = []
    manager._message_sequence = 1
    manager._system_entry = ContextEntry({"role": "system", "content": "system"}, None, -1)
    return manager


def test_message_context_persists_complete_ai_fields_and_ordered_parts() -> None:
    context_store = _RecordingContextStore()
    manager = _runtime_manager(context_store)
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

    persisted = context_store.appended[0]
    assert persisted["args"][2] == message


def test_begin_run_binds_identity_without_rewriting_context() -> None:
    """begin_run 只读库重建内存列表并绑定身份，不写任何 context 行。"""
    context_store = _RecordingContextStore()
    manager = _runtime_manager(context_store)
    message = {"role": "user", "content": "existing"}
    context_store.entries_in_context = lambda _task_id: [ContextEntry(message, 11, 1)]
    manager.begin_run(SimpleNamespace(id=12, task_id=7))
    assert manager.current_run_id == 12
    assert manager._entries[0].message is message
    assert context_store.appended == []


def test_sdk_response_projection_preserves_raw_tool_arguments(monkeypatch) -> None:
    from app.core.llm_provider.message_codec import assistant_message

    response = sdk_completion(
        "answer",
        reasoning_content="think",
        tool_calls=[
            {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{"}}
        ],
    )
    projected = assistant_message(response.choices[0].message, {"read_file"})
    assert projected["content"] == "answer"
    assert projected["reasoning_content"] == "think"
    assert projected["tool_calls"][0]["function"]["arguments"] == "{"


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


def test_orphan_recovery_commits_run_and_tool_closure_before_return() -> None:
    """恢复：Run 终态与工具收口在同一个事务内提交，提交后不再投影快照。"""

    order: list[str] = []
    recovered_run = SimpleNamespace(
        id=11, task_id=7, status="cancelled", execution_data=ReactExecutionState()
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
            clear_terminal_fields: bool = False,
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

    service = RecoverService.__new__(RecoverService)
    service._run = _RunCrud()
    service._context = _ContextService()
    service._session_factory = _SessionFactory()
    service._ledger = ConversationRunLedger.__new__(ConversationRunLedger)
    service._ledger._run = service._run

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
    service = ConversationRunLedger.__new__(ConversationRunLedger)
    service._run = store.runs
    service._task = store.tasks
    service._session_factory = store.factory
    monkeypatch.setattr(
        "app.core.conversation_run_ledger.get_conversation_event_projector", lambda: projector
    )
    monkeypatch.setattr(
        "app.core.conversation_run_ledger.get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _id: SimpleNamespace(
                supports_reasoning_effort=False,
                supports_image=False,
                context_window_k=128,
            )
        ),
    )
    monkeypatch.setattr(
        "app.service.conversation_run.user_input_service.get_model_config_service",
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
    """独立连接在事件发布时已能看到整组事实，context 留待模型边界并入。"""
    from app.core.workflow.inbox.task_message_queue import TaskMessageQueue
    from app.core.workflow.inbox.task_message import RunLaunch, TaskMessage, UserInputBody

    store = request.getfixturevalue("canonical_store")
    events = []

    class Projector:
        def process(self, event):
            assert store.tasks.get(store.task.id).current_run_id == event.run_id
            assert store.runs.get(event.run_id).context_window_total == 128000
            [queued] = TaskMessageQueue().pending_nowait(store.task.id)
            assert isinstance(queued.body, UserInputBody)
            assert queued.body.input_text == "hello"
            assert store.contexts.get(store.task.id, include_in_context=False) == []
            events.append(event)

    service = _creation_service(store, monkeypatch, Projector())
    body = UserInputBody(
        input_text="hello", display_text="hello", launch=RunLaunch(model_config_id=1)
    )
    TaskMessageQueue().put(store.task.id, TaskMessage.of(body))
    run = service.create_run(store.task.id, body.launch)
    assert len(events) == 1
    assert events[0].run_id == run.id


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
                "code": "max_steps_reached",
                "message": run_failure_message("max_steps_reached"),
            }
            run.status = "failed"
            return run

        def get(self, _run_id: int):
            return run

    class _Projector:
        def process(self, event: RunStatusChangedEvent, **kwargs: Any) -> None:
            order.append("event")
            assert event.status is ConversationRunStatus.FAILED

    service = ConversationRunLedger.__new__(ConversationRunLedger)
    service._run = _RunCrud()
    service._session_factory = None
    from app.core import conversation_run_ledger as ledger_module

    ledger_getter = ledger_module.get_conversation_event_projector
    ledger_module.get_conversation_event_projector = lambda: _Projector()
    try:
        result = service.transition(
            11, RunTransition.FAIL,
            end_reason="max_steps_reached",
            usage_stats=ConversationRunUsageStats(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cache_hit_tokens=1,
                cache_miss_tokens=2,
            ),
        )
    finally:
        ledger_module.get_conversation_event_projector = ledger_getter

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

    service = ConversationRunLedger.__new__(ConversationRunLedger)
    service._run = _RunCrud()
    from app.core import conversation_run_ledger as ledger_module

    original_getter = ledger_module.get_conversation_event_projector
    ledger_module.get_conversation_event_projector = lambda: _Projector()
    try:
        # 终态落库先于投影：投影失败向上传播，但已提交的 Run 事实完好。
        with pytest.raises(RuntimeError, match="transport unavailable"):
            service.transition(11, RunTransition.COMPLETE, final_output="done")
    finally:
        ledger_module.get_conversation_event_projector = original_getter

    assert committed == ["database"]
