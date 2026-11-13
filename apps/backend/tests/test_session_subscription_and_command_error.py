"""Session 级长订阅（attach）的 snapshot 契约。

订阅跟随 Session 而不是 Run：Session 没有 Run 时也推 full 帧并保持连接；Run 完成、新 Run 开始、取消或
挂起都不结束连接；只有客户端断开、``resync_required`` 或 Session 删除才结束。命令被拒由 HTTP 同步
返回，snapshot 没有顶层 ``error``。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.assistant_transport.service.conversation_state_service import (
    ConversationStateService,
)
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.session_sse_stream import SessionSseStream
from app.core.events import RunInitializedEvent, RunStatusChangedEvent
from app.models.enums.run_status import RunStatus
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.session_record import SessionRecord
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

# RunInitializedEvent 的 created_at 由领域侧从 Run 记录填入；测试给固定值。
_RUN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

pytestmark = pytest.mark.usefixtures("isolated_storage", "empty_tool_registry")

_SESSION_ID = 7


def _session() -> SessionRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return SessionRecord(
        id=_SESSION_ID,
        workspace_id=3,
        title="subscription task",
        created_at=now,
        updated_at=now,
        current_run_id=None,
    )


class _EmptySessionSources:
    """一个还没有任何 Run 与 context 的 Session。"""

    def get(
        self, _session_id: int, include_in_context: bool = True
    ) -> SessionRecord | list[SessionContextRecord]:
        return _session() if include_in_context else []

    def list_by_session(self, _session_id: int) -> list[RunRecord]:
        return []


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.session_runtime.session_runtime_space.get_session_service",
        lambda: SimpleNamespace(get_session=lambda _id: _session()),
    )
    monkeypatch.setattr(
        "app.session_runtime.session_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=3)),
    )
    session_runtime_spaces.close()
    yield
    session_runtime_spaces.close()


def _state_service() -> ConversationStateService:
    sources = _EmptySessionSources()
    with patch.multiple(
        "app.service.depends",
        get_session_crud=lambda: sources,
        get_run_crud=lambda: sources,
        get_session_context_crud=lambda: sources,
    ):
        return ConversationStateService()


def _stream_service(state_service: ConversationStateService) -> SessionSseStream:
    return SessionSseStream(state_service)


class _Collector:
    """在后台消费一条 attach 流，记录收到的帧；``cancel()`` 模拟客户端断开。

    静默边界（``None``）不是帧，单独记成「出现边界时已收到的帧数」。
    """

    def __init__(self, stream_service: SessionSseStream) -> None:
        self.frames: list[TransportFrame] = []
        self.idle_after: list[int] = []
        self._received = asyncio.Condition()
        self.task = asyncio.create_task(self._run(stream_service))

    async def _run(self, stream_service: SessionSseStream) -> None:
        async for frame in stream_service.frames(_SESSION_ID, poll_interval=0.01):
            async with self._received:
                if frame is None:
                    self.idle_after.append(len(self.frames))
                else:
                    self.frames.append(frame)
                self._received.notify_all()

    async def disconnect(self) -> None:
        """客户端断开：生产里由 Starlette 取消订阅协程，订阅随之撤下。"""
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.task, 1)

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
# Session 级长订阅（C1 / C2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_without_run_pushes_full_frame_and_stays_open() -> None:
    collector = _Collector(_stream_service(_state_service()))
    await collector.wait_frames(1)

    first = collector.frames[0]
    assert first.kind == "full"
    assert first.state == {
        "title": "subscription task",
        "messages": [],
        "messageOffset": 0,
        "activity": None,
        "terminal_sessions": [],
        "background_tasks": [],
        "foreground_waits": [],
        "handoffs": [],
        "queued_messages": [],
    }
    await _settle()
    assert not collector.task.done()

    await collector.disconnect()
    assert len(collector.frames) == 1


@pytest.mark.asyncio
async def test_one_connection_survives_run_completion_new_run_cancel_and_suspend() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    def _status(run_id: int, status: RunStatus) -> None:
        service.apply_planned(RunStatusChangedEvent(changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), session_id=_SESSION_ID, run_id=run_id, status=status))

    service.apply_planned(
        RunInitializedEvent(created_at=_RUN_CREATED_AT, session_id=_SESSION_ID, run_id=1)
    )
    _status(1, RunStatus.RUNNING)
    _status(1, RunStatus.COMPLETED)
    await _settle()
    assert not collector.task.done()

    # 同一条连接上看到新 Run 开始、挂起、唤醒与取消。
    service.apply_planned(
        RunInitializedEvent(created_at=_RUN_CREATED_AT, session_id=_SESSION_ID, run_id=2)
    )
    _status(2, RunStatus.RUNNING)
    _status(2, RunStatus.SUSPENDED)
    await _settle()
    assert not collector.task.done()
    _status(2, RunStatus.RUNNING)
    _status(2, RunStatus.CANCELLED)
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
    activity = service.get_state(_SESSION_ID)["activity"]
    assert activity is not None
    assert activity["status"] == "cancelled"
    assert all(frame.kind == "mutation" for frame in collector.frames[1:])

    await collector.disconnect()


@pytest.mark.asyncio
async def test_idle_boundary_follows_each_burst_exactly_once() -> None:
    """WebKit 会扣住静默前的最后一段数据：每批帧之后必须恰好有一次边界，持续空闲不重复。"""
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)
    await _settle()
    # 首个 full 帧之后同样需要边界，否则 attach 的首帧也可能被扣住。
    assert collector.idle_after == [1]

    service.apply_planned(
        RunInitializedEvent(created_at=_RUN_CREATED_AT, session_id=_SESSION_ID, run_id=1)
    )
    service.apply_planned(
        RunStatusChangedEvent(changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), session_id=_SESSION_ID, run_id=1, status=RunStatus.RUNNING)
    )
    service.apply_planned(
        RunStatusChangedEvent(changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), session_id=_SESSION_ID, run_id=1, status=RunStatus.COMPLETED)
    )
    await _settle()

    # 同一批帧连续取出、中间不插边界；整批发完后（含终态帧）只补一次。
    assert len(collector.frames) > 2
    assert collector.idle_after == [1, len(collector.frames)]

    await collector.disconnect()


@pytest.mark.asyncio
async def test_envelopes_encode_idle_boundary_as_sse_comment() -> None:
    """边界编码为 SSE 注释行：客户端只读 ``data:`` 记录，会跳过它，只起推出前文的作用。"""
    envelopes = _stream_service(_state_service()).sse_records(_SESSION_ID)

    first = await anext(envelopes)
    boundary = await asyncio.wait_for(anext(envelopes), 1)
    await envelopes.aclose()

    assert first.startswith("data: ")
    assert boundary == ": flush\n\n"
    assert not any(line.startswith("data:") for line in boundary.splitlines())


@pytest.mark.asyncio
async def test_attach_ends_on_resync_required_frame() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    service._publish(
        TransportFrame(
            session_id=_SESSION_ID,
            kind="resync_required",
            mutations=(),
            resync_reason="subscriber_backpressure",
        )
    )

    await asyncio.wait_for(collector.task, 1)
    assert collector.frames[-1].kind == "resync_required"


@pytest.mark.asyncio
async def test_attach_ends_when_session_is_deleted() -> None:
    service = _state_service()
    collector = _Collector(_stream_service(service))
    await collector.wait_frames(1)

    session_runtime_spaces.mark_deleted(_SESSION_ID)
    service.release_deleted_session(_SESSION_ID)

    await asyncio.wait_for(collector.task, 1)
    assert len(collector.frames) == 1


@pytest.mark.asyncio
async def test_attach_envelopes_encode_session_view_frames_without_run_fields() -> None:
    import json

    service = _state_service()
    stream_service = _stream_service(service)
    envelopes = stream_service.sse_records(_SESSION_ID)

    first = json.loads((await anext(envelopes)).removeprefix("data: "))
    await envelopes.aclose()

    assert first["kind"] == "full"
    assert first["state"] == {
        "title": "subscription task",
        "messages": [],
        "messageOffset": 0,
        "activity": None,
        "terminal_sessions": [],
        "background_tasks": [],
        "foreground_waits": [],
        "handoffs": [],
        "queued_messages": [],
    }
    assert set(first) == {"session_id", "kind", "mutations", "resync_reason", "state"}
