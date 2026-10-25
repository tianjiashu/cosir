"""Task 级长订阅（attach）与顶层命令错误的 snapshot 契约。

订阅跟随 Task 而不是 Run：Task 没有 Run 时也推 full 帧并保持连接；Run 完成、新 Run 开始、取消或
挂起都不结束连接；只有客户端断开、``resync_required`` 或 Task 删除才结束。顶层 ``error`` 只由
``set_command_error`` 写入 / 清空，Run 状态事件不会写它。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.assistant_transport.stream import TransportFrame
from app.core.events import RunInitializedEvent, RunStatusChangedEvent
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.task_record import TaskRecord

pytestmark = pytest.mark.usefixtures("isolated_storage")

_TASK_ID = 7


def _task() -> TaskRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return TaskRecord(
        id=_TASK_ID,
        workspace_id=3,
        title="subscription task",
        created_at=now,
        updated_at=now,
        current_run_id=None,
    )


class _EmptyTaskSources:
    """一个还没有任何 Run 与 context 的 Task。"""

    def get(
        self, _task_id: int, include_in_context: bool = True
    ) -> TaskRecord | list[ConversationTaskContextRecord]:
        return _task() if include_in_context else []

    def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
        return []


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_task_service",
        lambda: SimpleNamespace(get_task=lambda _id: _task()),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=3)),
    )
    ConversationTaskStateService.clear_process_state()
    yield
    ConversationTaskStateService.clear_process_state()


def _state_service() -> ConversationTaskStateService:
    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    sources = _EmptyTaskSources()
    service._task_source = sources
    service._run_source = sources
    service._context_source = sources
    return service


def _stream_service(state_service: ConversationTaskStateService) -> AssistantTransportStreamService:
    stream_service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    stream_service._snapshots = state_service
    return stream_service


class _Collector:
    """在后台消费一条 attach 流，记录收到的帧；``cancel()`` 模拟客户端断开。"""

    def __init__(self, stream_service: AssistantTransportStreamService) -> None:
        self.frames: list[TransportFrame] = []
        self._cancelled = False
        self._received = asyncio.Condition()
        self.task = asyncio.create_task(self._run(stream_service))

    async def _run(self, stream_service: AssistantTransportStreamService) -> None:
        async for frame in stream_service.stream(
            _TASK_ID, lambda: self._cancelled, poll_interval=0.01
        ):
            async with self._received:
                self.frames.append(frame)
                self._received.notify_all()

    def cancel(self) -> None:
        self._cancelled = True

    async def wait_frames(self, count: int) -> None:
        async with self._received:
            await asyncio.wait_for(
                self._received.wait_for(lambda: len(self.frames) >= count), 1
            )


async def _settle() -> None:
    """让 poll 循环多转几圈：证明连接没有因为没有帧或 Run 变化而自行结束。"""

    await asyncio.sleep(0.05)


def _statuses(frames: list[TransportFrame]) -> list[object]:
    return [
        mutation.value
        for frame in frames
        for mutation in frame.mutations
        if mutation.path == ("activity", "status")
    ]


# ---------------------------------------------------------------------------
# 顶层命令错误（A6：只由 set_command_error 写入 / 清空）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_command_error_assigns_new_seq_per_rejection_and_clears() -> None:
    """每次拒绝都分配新的 ``seq`` 并发帧（原因相同也发），前端靠它认出「又被拒了一次」；
    清空只在有错误时发帧。"""
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)
    error = {"code": "TASK_STATE_CONFLICT", "message": "当前状态不允许执行该操作"}

    service.set_command_error(_TASK_ID, error)
    await collector.wait_frames(2)
    first = service.get_state(_TASK_ID)["error"]
    assert first is not None
    assert {k: v for k, v in first.items() if k != "seq"} == error
    assert collector.frames[1].mutations[0].path == ("error",)
    assert collector.frames[1].mutations[0].value == first

    # 同样的原因再被拒一次：值只差 seq，照样发帧。
    service.set_command_error(_TASK_ID, dict(error))
    await collector.wait_frames(3)
    second = service.get_state(_TASK_ID)["error"]
    assert second is not None and second["seq"] > first["seq"]

    service.set_command_error(_TASK_ID, None)
    await collector.wait_frames(4)
    assert service.get_state(_TASK_ID)["error"] is None
    assert collector.frames[3].mutations[0].value is None

    # 清空后再清空不发帧。
    service.set_command_error(_TASK_ID, None)
    await _settle()
    assert len(collector.frames) == 4

    collector.cancel()
    await asyncio.wait_for(collector.task, 1)


def test_run_status_events_keep_command_error() -> None:
    """Run 终态（含失败）只写 activity / outcome，不覆盖或清空命令错误。"""

    service = _state_service()
    error = {"code": "RESUME_REJECTED", "message": "当前没有可恢复的 Run"}
    service.set_command_error(_TASK_ID, error)

    service.apply_planned(RunInitializedEvent(task_id=_TASK_ID, run_id=1))
    service.apply_planned(
        RunStatusChangedEvent(
            task_id=_TASK_ID, run_id=1, status=ConversationRunStatus.RUNNING
        )
    )
    service.apply_planned(
        RunStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=1,
            status=ConversationRunStatus.FAILED,
            end_reason="runtime_failed",
        )
    )

    state = service.get_state(_TASK_ID)
    assert state["activity"] is not None
    assert state["activity"]["status"] == "failed"
    assert state["error"] is not None
    assert (state["error"]["code"], state["error"]["message"]) == (error["code"], error["message"])


def test_rebuild_keeps_command_error() -> None:
    """命令错误不是 canonical 事实：强制重建（新开 Run、编辑重跑）要把它原样带入新副本，
    只有下一次成功的用户命令才能清空它。"""

    service = _state_service()
    service.set_command_error(_TASK_ID, {"code": "RUN_NOT_RESUMABLE", "message": "不可恢复"})
    before = service.get_state(_TASK_ID)["error"]

    assert service.rebuild_state(_TASK_ID)["error"] == before
    assert service.get_state(_TASK_ID)["error"] == before


# ---------------------------------------------------------------------------
# Task 级长订阅（C1 / C2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_without_run_pushes_full_frame_and_stays_open() -> None:
    collector = _Collector(_stream_service(_state_service()))
    await collector.wait_frames(1)

    first = collector.frames[0]
    assert first.kind == "full"
    assert first.state == {
        "messages": [],
        "activity": None,
        "error": None,
        "queued_messages": [],
    }
    await _settle()
    assert not collector.task.done()

    collector.cancel()
    await asyncio.wait_for(collector.task, 1)
    assert len(collector.frames) == 1


@pytest.mark.asyncio
async def test_one_connection_survives_run_completion_new_run_cancel_and_suspend() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    def _status(run_id: int, status: ConversationRunStatus) -> None:
        service.apply_planned(RunStatusChangedEvent(task_id=_TASK_ID, run_id=run_id, status=status))

    service.apply_planned(RunInitializedEvent(task_id=_TASK_ID, run_id=1))
    _status(1, ConversationRunStatus.RUNNING)
    _status(1, ConversationRunStatus.COMPLETED)
    await _settle()
    assert not collector.task.done()

    # 同一条连接上看到新 Run 开始、挂起、唤醒与取消。
    service.apply_planned(RunInitializedEvent(task_id=_TASK_ID, run_id=2))
    _status(2, ConversationRunStatus.RUNNING)
    _status(2, ConversationRunStatus.SUSPENDED)
    await _settle()
    assert not collector.task.done()
    _status(2, ConversationRunStatus.RUNNING)
    _status(2, ConversationRunStatus.CANCELLED)
    await _settle()
    assert not collector.task.done()

    assert _statuses(collector.frames[1:]) == [
        "running",
        "completed",
        "running",
        "suspended",
        "running",
        "cancelled",
    ]
    activity = service.get_state(_TASK_ID)["activity"]
    assert activity is not None
    assert (activity["runId"], activity["status"]) == (2, "cancelled")
    assert all(frame.kind == "mutation" for frame in collector.frames[1:])

    collector.cancel()
    await asyncio.wait_for(collector.task, 1)


@pytest.mark.asyncio
async def test_attach_ends_on_resync_required_frame() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    service._publish(
        TransportFrame(
            task_id=_TASK_ID,
            kind="resync_required",
            mutations=(),
            resync_reason="subscriber_backpressure",
        )
    )

    await asyncio.wait_for(collector.task, 1)
    assert collector.frames[-1].kind == "resync_required"


@pytest.mark.asyncio
async def test_attach_ends_when_task_is_deleted() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    service.mark_task_deleted(_TASK_ID)

    await asyncio.wait_for(collector.task, 1)
    assert len(collector.frames) == 1


@pytest.mark.asyncio
async def test_attach_envelopes_encode_task_view_frames_without_run_fields() -> None:
    import json

    service = _state_service()
    stream_service = _stream_service(service)
    envelopes = stream_service.stream_envelopes(_TASK_ID, lambda: False)

    first = json.loads((await anext(envelopes)).removeprefix("data: "))
    await envelopes.aclose()

    assert first["kind"] == "full"
    assert first["state"] == {
        "messages": [],
        "activity": None,
        "error": None,
        "queued_messages": [],
    }
    assert set(first) == {"task_id", "kind", "mutations", "resync_reason", "state"}
