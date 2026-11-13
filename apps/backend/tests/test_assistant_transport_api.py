import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.sessions_api import get_session
from app.app import app
from app.assistant_transport.api import (
    assistant_history_page,
    assistant_transport_attach,
)
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.enums.run_status import RunTransition
from app.models.session_extra import SessionExtra
from app.service.depends import get_workspace_service
from app.service.recover_service import RecoverService
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


def test_session_route_has_one_get_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/sessions/{session_id}"
        and route.methods == {"GET"}
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_session_endpoint_returns_session_response_without_session_level_fork_gate() -> None:
    now = datetime.now(UTC)
    record = SimpleNamespace(
        id=7,
        workspace_id=3,
        title="run",
        extra=SessionExtra(),
        current_run_id=None,
        context_window_total=None,
        session_type="user",
        parent_session_id=None,
        created_at=now,
        updated_at=now,
    )

    class _SessionService:
        def get_session(self, _session_id: int) -> object:
            return record

        def get_context_window_total(self, _session_id: int) -> int | None:
            return None

    result = await get_session(7, _SessionService())

    assert result.session_id == 7
    assert result.workspace_id == 3
    assert "fork_available" not in result.model_dump()


def test_attach_route_is_separate_from_business_resume() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/sessions/{session_id}/assistant/attach"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"POST"}


class _AttachService:
    """只认 Session、返回 SSE 记录的替身：attach 路由只负责把记录包成长连接响应。"""

    def attach(self, _session_id: int):
        async def records():
            yield "data: {}\n\n"

        return records()


def test_attach_subscribes_by_session_as_an_event_stream() -> None:
    """attach 只按 Session 订阅，不需要也不接受 run_id；响应是不缓存的 SSE 长连接。"""

    response = asyncio.run(assistant_transport_attach(1, service=_AttachService()))

    assert response.media_type == "text/event-stream"
    assert response.headers["X-Cosir-Session-Id"] == "1"
    assert response.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/sessions/{session_id}/messages",
        "/sessions/{session_id}/queued-messages/{message_id}",
        "/sessions/{session_id}/messages/{message_id}/withdraw",
        "/sessions/{session_id}/resume",
        "/sessions/{session_id}/tool-calls/{tool_call_id}/decision",
        "/sessions/{session_id}/cancel",
        "/sessions/{session_id}/tool-calls/{tool_call_id}/cancel",
    ],
)
def test_session_commands_are_addressed_by_session(path: str) -> None:
    """命令只对 Session 或消息寻址；统一入口 ``/assistant`` 与按 run 寻址的端点不复存在。"""

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert path in paths
    assert "/assistant" not in paths
    assert not any(p.startswith("/runs/") for p in paths)


def test_restart_recovery_cancels_run_and_closes_tool_calls() -> None:
    """重启恢复只写持久化事实（Run 终态 + 工具收口），不触碰 snapshot。"""

    calls: list[tuple[int, RunTransition, str | None]] = []
    repaired: list[tuple[int, int]] = []

    class _Transaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class _DbFactory:
        def begin(self) -> _Transaction:
            return _Transaction()

    class _RunCrud:
        def list_recoverable(self) -> list[object]:
            return [
                SimpleNamespace(id=run_id, session_id=session_id, execution_data=ReactExecutionState())
                for run_id, session_id in ((7, 1), (8, 2))
            ]

    class _RunStateService:
        def transition(
            self,
            run_id: int,
            transition: RunTransition,
            *,
            end_reason: str | None = None,
            execution_data: object = None,
            db: object = None,
        ) -> object:
            assert db is not None
            calls.append((run_id, transition, end_reason))
            return SimpleNamespace(id=run_id)

    class _ContextService:
        def close_unclosed_tool_calls_for_run(
            self, session_id: int, run_id: int, db: object = None
        ) -> list[str]:
            assert db is not None
            repaired.append((session_id, run_id))
            return [f"call-{run_id}"]

    service = RecoverService.__new__(RecoverService)
    service._run = _RunCrud()
    service._ledger = _RunStateService()
    service._context = _ContextService()
    service._db_factory = _DbFactory()

    result = service.recover_orphaned_runs()

    assert [record.id for record in result] == [7, 8]
    assert calls == [
        (7, RunTransition.CANCEL, "runtime_restarted"),
        (8, RunTransition.CANCEL, "runtime_restarted"),
    ]
    assert repaired == [(1, 7), (2, 8)]


@pytest.mark.asyncio
async def test_history_page_maps_a_stale_position_to_409() -> None:
    class _TransportService:
        def get_history_page(self, _session_id: int, end: int) -> dict[str, object]:
            if end > 3:
                raise ValueError("stale")
            return {"messages": [], "messageOffset": 0}

    assert await assistant_history_page(1, 3, _TransportService()) == {
        "messages": [],
        "messageOffset": 0,
    }
    with pytest.raises(HTTPException) as caught:
        await assistant_history_page(1, 9, _TransportService())
    assert caught.value.status_code == 409
    assert caught.value.detail["error"]["code"] == "HISTORY_STALE"


async def test_session_gate_waits_without_blocking_event_loop(tmp_path: Path) -> None:
    """闸门排队必须发生在工作线程：等待期间事件循环仍要能调度其它协程。

    Session 闸门要跨越 ``SessionActor`` 一批信号处理内的 ``await``；若用同步
    ``operation()`` 在事件循环线程上抢锁，同一 session 的并发请求会把事件循环整体卡在
    ``acquire`` 上，而持锁者又必须回到事件循环才能释放闸门，等待者遂只能等满超时。
    本用例断言等待期间心跳协程仍在推进。
    """

    workspace_service = get_workspace_service()
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = workspace_service.create_workspace("gate-test", str(root))
    session = workspace_service.create_session(workspace.id, "gate-test")
    space = session_runtime_spaces.get_or_create(session.id)
    acquired = threading.Event()
    release = threading.Event()

    def _hold_gate() -> None:
        with space.operation(timeout=5):
            acquired.set()
            # 超时仅为错误实现保底解锁；正常路径由事件循环中的心跳释放。
            release.wait(3)

    holder = threading.Thread(target=_hold_gate, daemon=True)
    holder.start()
    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        release.set()

    beating = None
    try:
        assert await asyncio.to_thread(acquired.wait, 3)
        beating = asyncio.create_task(_heartbeat())
        async with space.async_operation(wait_seconds=10):
            # 必须由心跳推进到主动释放，不能依靠持锁线程的保底超时。
            assert ticks == 5
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        if beating is not None:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)
        session_runtime_spaces.unload(session.id)

    assert not holder.is_alive()


# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")
