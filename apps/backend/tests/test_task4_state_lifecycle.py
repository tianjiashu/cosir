"""canonical 冷读、TaskRuntimeSpace 懒加载与快照进程生命周期。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.stream.task_sse_stream import TaskSseStream
from app.core.events import RunStatusChangedEvent
from app.core.workflow import conversation_run_ledger as conversation_run_ledger_module
from app.core.workflow.conversation_run_ledger import ConversationRunLedger
from app.models import ConversationRunStatus
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_failure import run_failure_message, run_terminal_error
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import RunTransition
from app.models.message_metadata import UserMessageMetadata
from app.models.task_record import TaskRecord
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

# 任务运行时空间在构造时会读当前 task，因此本文件全部用例都需要隔离主库。
pytestmark = pytest.mark.usefixtures("isolated_storage")


def _user_meta(text: str) -> UserMessageMetadata:
    """落库的用户消息都带来源 ``kind``；用户输入还带展示绑定（snapshot 重建与 fork 只从这里取）。"""
    return UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(text=text))


def _task() -> TaskRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return TaskRecord(
        id=7,
        workspace_id=3,
        title="canonical task",
        created_at=now,
        updated_at=now,
        current_run_id=2,
    )


def _activity(run_id: int, status: str) -> dict[str, object]:
    return {
        "modelActivity": None,
        "turn": None,
        "resumable": False,
        "status": status,
        "endReason": None,
        "error": None,
        "usage": None,
        "contextWindowTotal": None,
        "startedAt": "2026-01-01T00:00:00+00:00",
        "launch": {"modelConfigId": None, "reasoningEffort": None, "disabledToolGroups": []},
    }


def _run(run_id: int, status: str) -> ConversationRunRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return ConversationRunRecord(
        id=run_id,
        task_id=7,
        context_window_total=100,
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _reset_task_runtime_spaces(monkeypatch, backend_env) -> None:
    """隔离任务服务依赖与进程缓存，保留真实 TaskRuntimeSpace 的懒加载行为。"""

    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_task_service",
        lambda: SimpleNamespace(get_task=lambda _id: _task()),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=3)),
    )
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()


def _make_state_service(
    task_source: object, run_source: object, context_source: object
) -> ConversationTaskStateService:
    """按生产构造装配一个状态服务；构造器从 depends 读三个来源，这里临时换成给定的来源。"""

    with patch.multiple(
        "app.service.depends",
        get_task_crud=lambda: task_source,
        get_conversation_run_crud=lambda: run_source,
        get_conversation_task_context_crud=lambda: context_source,
    ):
        return ConversationTaskStateService()


def test_cold_state_rebuild_reads_only_task_runs_and_context() -> None:
    task = _task()
    runs = [_run(2, "completed"), _run(1, "failed")]
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message={"role": "user", "content": "from canonical context"},
            metadata=_user_meta("from canonical context"),
            include_in_context=True,
            sequence=1,
            turn=1,
        )
    ]

    class CanonicalSources:
        def get(
            self, task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            assert task_id == 7
            return task if include_in_context else rows

        def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
            assert task_id == 7
            return sorted(runs, key=lambda run: run.id)

    sources = CanonicalSources()
    service = _make_state_service(sources, sources, sources)

    state = service.get_state(7)

    assert state["activity"] is not None
    assert state["activity"]["turn"] == 1
    # 由输入开启的回合总有回复；正文取对应 context，不能重复使用 Run 的首条 text。
    assert [message["turn"] for message in state["messages"]] == [1, 1]
    assert state["messages"][0]["parts"][0]["text"] == "from canonical context"


def test_cold_state_rebuild_ignores_task_system_prompt() -> None:
    """快照重建应忽略 Task 系统提示词，只投影绑定 Run 的会话消息。"""

    task = _task()
    run = _run(2, "completed")
    rows = [
        ConversationTaskContextRecord(
            id=10,
            task_id=7,
            run_id=None,
            message={"role": "system", "content": "internal system prompt"},
            include_in_context=True,
            sequence=0,
            turn=1,
        ),
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message={"role": "user", "content": "canonical user message"},
            metadata=_user_meta("canonical user message"),
            include_in_context=True,
            sequence=1,
            turn=1,
        ),
        ConversationTaskContextRecord(
            id=12,
            task_id=7,
            run_id=2,
            message={"role": "assistant", "content": "canonical assistant message"},
            include_in_context=True,
            sequence=2,
            turn=1,
        ),
    ]

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [run]

    service = _make_state_service(CanonicalSources(), CanonicalSources(), CanonicalSources())

    state = service.get_state(7)

    messages = state["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["parts"][0]["text"] == "canonical assistant message"


def test_state_snapshot_is_lazily_rebuilt_once_per_task_runtime_space() -> None:
    task_runtime_spaces.close()
    task_runtime_spaces.close()
    task = _task()
    run = _run(2, "completed")
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message={"role": "user", "content": "durable"},
            metadata=_user_meta("durable"),
            include_in_context=True,
            sequence=1,
            turn=1,
        )
    ]
    reads = {"task": 0, "runs": 0, "context": 0}

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            reads["task" if include_in_context else "context"] += 1
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            reads["runs"] += 1
            return [run]

    try:
        sources = CanonicalSources()
        first_service = _make_state_service(sources, sources, sources)
        second_service = _make_state_service(sources, sources, sources)

        first = first_service.get_state(7)
        second = first_service.get_state(7)
        third = second_service.get_state(7)

        assert first == second == third
        assert reads == {"task": 1, "runs": 1, "context": 1}
        space = task_runtime_spaces.get(7)
        assert space is not None
        assert space.has_snapshot()
    finally:
        task_runtime_spaces.close()
        task_runtime_spaces.close()


def test_memory_state_is_not_written_to_persistence_and_rebuilds_after_restart() -> None:
    task = _task()
    run = _run(2, "completed")
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message={"role": "user", "content": "durable"},
            metadata=_user_meta("durable"),
            include_in_context=True,
            sequence=1,
            turn=1,
        )
    ]

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [run]

    service = _make_state_service(CanonicalSources(), CanonicalSources(), CanonicalSources())
    first = service.get_state(7)
    assert first["activity"] is not None
    assert first["activity"]["status"] == "completed"
    assert service.get_state(7) == first

    task_runtime_spaces.close()
    restarted = _make_state_service(CanonicalSources(), CanonicalSources(), CanonicalSources())
    rebuilt = restarted.get_state(7)
    assert rebuilt["messages"][0]["parts"][0]["text"] == "durable"


def test_canonical_state_sources_failures_are_logged_and_not_replaced_by_empty_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenSources:
        def get(self, _task_id: int) -> TaskRecord:
            raise RuntimeError("canonical read failed")

    service = _make_state_service(BrokenSources(), BrokenSources(), BrokenSources())

    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="canonical read failed"):
        service.get_state(7)

    assert "state_rebuild_started" in {record.message for record in caplog.records}
    assert "state_rebuild_failed" in {record.message for record in caplog.records}


def test_get_state_returns_preinstalled_snapshot_without_rebuild() -> None:
    task = _task()
    run = _run(2, "running")
    rows = [
        ConversationTaskContextRecord(
            id=11,
            task_id=7,
            run_id=2,
            message={"role": "user", "content": "committed user"},
            metadata=_user_meta("committed user"),
            include_in_context=True,
            sequence=1,
            turn=1,
        ),
        ConversationTaskContextRecord(
            id=12,
            task_id=7,
            run_id=2,
            message={"role": "assistant", "content": "committed assistant fact"},
            include_in_context=True,
            sequence=2,
            turn=1,
        ),
    ]

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return task if include_in_context else rows

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [run]

    service = _make_state_service(CanonicalSources(), CanonicalSources(), CanonicalSources())
    task_runtime_spaces.get_or_create(7).replace_snapshot(
        {
            "title": "task",
            "messages": [
                {
                    "id": "stale-user",
                    "role": "user",
                    "parts": [{"type": "text", "text": "stale", "status": "completed"}],
                    "turn": 2,
                    "inherited": False,
                    "outcome": None,
                    "source": None,
                },
                {
                    "id": "stale-assistant",
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "stale", "status": "running"}],
                    "turn": 2,
                    "inherited": False,
                    "outcome": None,
                    "source": None,
                },
            ],
            "messageOffset": 0,
            "activity": _activity(2, "running"),
            "terminal_sessions": [],
            "background_tasks": [],
            "foreground_waits": [],
            "handoffs": [],
            "queued_messages": [],
        }
    )

    state = service.get_state(7)

    assert [message["id"] for message in state["messages"]] == [
        "stale-user",
        "stale-assistant",
    ]


def test_transition_propagates_post_commit_projector_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态迁移在原子更新成功之后投影失败必须向上传播（与 create_run 一致）。"""

    class RunCrud:
        def update_status_if_in(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(task_id=7, updated_at=datetime(2026, 9, 12, tzinfo=UTC))

        def get(self, _run_id: int) -> SimpleNamespace:
            return SimpleNamespace(status="running")

    class FailingProjector:
        def process(self, _event: object) -> None:
            raise RuntimeError("transport unavailable")

    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector",
        lambda: FailingProjector(),
    )
    service = ConversationRunLedger.__new__(ConversationRunLedger)
    service._run = RunCrud()

    with pytest.raises(RuntimeError, match="transport unavailable"):
        service.transition(7, RunTransition.COMPLETE)
    monkeypatch.undo()


def test_malformed_context_deserialization_fails_at_state_read_boundary() -> None:
    class MalformedContextSource:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            if include_in_context:
                return _task()
            raise ValueError("malformed persisted context JSON")

    class CanonicalRuns:
        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return []

    service = _make_state_service(
        MalformedContextSource(), CanonicalRuns(), MalformedContextSource()
    )

    with pytest.raises(ValueError, match="malformed persisted context"):
        service.get_state(7)


def test_persisted_snapshot_model_and_crud_are_removed() -> None:
    backend_app = Path(__file__).parents[1] / "app"
    assert not (backend_app / "storage/model/conversation_task_snapshot_model.py").exists()
    assert not (backend_app / "storage/crud/conversation_task_snapshot_crud.py").exists()


@pytest.mark.asyncio
async def test_cold_sse_first_frame_uses_canonical_rebuild() -> None:
    """The first SSE frame must come from canonical records when no working copy exists."""

    task_runtime_spaces.close()

    class CanonicalSources:
        def get(
            self, _task_id: int, include_in_context: bool = True
        ) -> TaskRecord | list[ConversationTaskContextRecord]:
            return _task() if include_in_context else []

        def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
            return [_run(2, "completed")]

    state_service = _make_state_service(CanonicalSources(), CanonicalSources(), CanonicalSources())
    stream = TaskSseStream(state_service).frames(7)
    first = await anext(stream)
    await stream.aclose()

    assert first.kind == "full"
    assert first.state["activity"]["status"] == "completed"


# Run 级错误契约的键集精确锁定为 code + message，任何往里加字段（如 retryable）的改动都必须
# 让本用例变红。
def test_terminal_error_contract_carries_only_code_and_message() -> None:
    failed = run_terminal_error(
        ConversationRunStatus.FAILED,
        "max_steps_reached",
    )
    cancelled = run_terminal_error(
        ConversationRunStatus.CANCELLED,
        "user cancelled mid-run",  # 非合法标识符 -> 回退固定 code
    )

    # message 由失败 code 目录统一产出（provider 无关文案）；这里同时锁定 code 与「message
    # 只来自目录」这一契约，避免有人把文案抄回调用点。
    assert failed == {
        "code": "max_steps_reached",
        "message": run_failure_message("max_steps_reached"),
    }
    assert set(failed or {}) == {"code", "message"}
    assert cancelled == {
        "code": "run_cancelled",
        "message": run_failure_message("run_cancelled"),
    }
    assert (
        run_terminal_error(
            ConversationRunStatus.COMPLETED,
            None,
        )
        is None
    )


class _RunSources:
    """一个 Task、一条 Run、没有 context 行；Run 状态可改。"""

    def __init__(self, status: str) -> None:
        self.run = _run(2, status)

    def get(self, _task_id: int, include_in_context: bool = True) -> object:
        return _task() if include_in_context else []

    def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
        return [self.run]


@pytest.mark.asyncio
async def test_snapshot_unloads_when_last_subscriber_leaves_an_idle_task() -> None:
    sources = _RunSources("completed")
    service = _make_state_service(sources, sources, sources)
    _, first_unsubscribe, _ = service.subscribe_with_snapshot(7)
    _, second_unsubscribe, _ = service.subscribe_with_snapshot(7)
    space = task_runtime_spaces.get(7)
    assert space is not None and space.has_snapshot()

    first_unsubscribe()
    assert space.has_snapshot()
    second_unsubscribe()
    assert not space.has_snapshot()


@pytest.mark.asyncio
async def test_running_snapshot_is_kept_until_the_run_stops_unwatched() -> None:
    """流式正文没落库：运行中不卸载，Run 停下且无人订阅时再卸载。"""
    sources = _RunSources("running")
    service = _make_state_service(sources, sources, sources)
    _, unsubscribe, _ = service.subscribe_with_snapshot(7)
    unsubscribe()
    space = task_runtime_spaces.get(7)
    assert space is not None and space.has_snapshot()

    sources.run = _run(2, "completed")
    service.apply_planned(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=2,
            status=ConversationRunStatus.COMPLETED,
        )
    )
    assert not space.has_snapshot()
