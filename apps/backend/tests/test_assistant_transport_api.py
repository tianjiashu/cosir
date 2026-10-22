import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.tasks_api import get_task
from app.app import app
from app.assistant_transport.assistant_api import assistant_transport_state
from app.assistant_transport.request.part import AssistantImagePart, AssistantTextPart
from app.assistant_transport.request.task_command_request import _ordered_display_text
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
)
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.conversation_run.workflow_session import WorkflowSession
from app.service.depends import get_workspace_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


def _snapshot(
    run_id: int | None = None, status: str = "idle", end_reason: str | None = None
) -> dict[str, object]:
    runs = (
        []
        if run_id is None
        else [
            {
                "runId": run_id,
                "status": status,
                "endReason": end_reason,
                "messages": [{"id": f"user-{run_id}", "role": "user", "parts": []}],
                "usage": None,
                "error": None,
            }
        ]
    )
    return {
        "runs": runs,
        "current_run_id": run_id,
        "approvals": {},
        "context_window_total": None,
        "error": None,
    }


def test_ordered_display_text_encodes_image_between_text_parts() -> None:
    """Transport 入口必须把 composer 中图片位置编码进既有展示文本。"""

    image_id = "b" * 64
    display_text = _ordered_display_text(
        [
            AssistantTextPart(type="text", text="前"),
            AssistantImagePart(type="image", image=f"cosir-attachment://{image_id}"),
            AssistantTextPart(type="text", text="后"),
        ]
    )

    assert display_text == f"前\n[[cosir-image:{image_id}]]\n后"


def test_ordered_display_text_does_not_duplicate_image_marker_from_edit_text() -> None:
    """编辑草稿已有图片 marker 时，附件 parts 不能再追加第二个 marker。"""

    image_id = "c" * 64
    display_text = _ordered_display_text(
        [
            AssistantTextPart(type="text", text=f"前[[cosir-image:{image_id}]]后"),
            AssistantImagePart(type="image", image=f"cosir-attachment://{image_id}"),
        ]
    )

    assert display_text == f"前[[cosir-image:{image_id}]]后"


def test_task_route_has_one_get_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/tasks/{task_id}"
        and route.methods == {"GET"}
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_task_endpoint_returns_task_response_without_task_level_fork_gate() -> None:
    now = datetime.now(UTC)
    record = SimpleNamespace(
        id=7,
        workspace_id=3,
        title="conversation_run",
        extra=None,
        execution_status=None,
        current_run_id=None,
        context_window_total=None,
        task_type="user",
        parent_task_id=None,
        created_at=now,
        updated_at=now,
    )

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return record

        def get_context_window_total(self, _task_id: int) -> int | None:
            return None

    result = await get_task(7, _TaskService())

    assert result.task_id == 7
    assert result.workspace_id == 3
    assert "fork_available" not in result.model_dump()


def test_attach_route_is_separate_from_business_resume() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/tasks/{task_id}/assistant/attach"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"POST"}


def _attach_service(state: dict[str, object]) -> TransportAssistantService:
    class _SnapshotService:
        def get_state(self, _task_id: int) -> dict[str, object]:
            return state

    async def _stream_envelopes(*_args: object, **_kwargs: object):
        yield "data: {}\n\n"

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._snapshots = _SnapshotService()
    service._stream = SimpleNamespace(stream_envelopes=_stream_envelopes)
    return service


def test_attach_subscribes_task_current_run() -> None:
    """attach 只按 Task 订阅当前 Run，不需要也不接受 run_id。"""

    result = _attach_service(_snapshot(7, "running")).attach(1)

    assert result.media_type == "text/event-stream"
    assert result.headers["X-Cosir-Task-Id"] == "1"


def test_attach_without_any_run_is_conflict() -> None:
    with pytest.raises(HTTPException) as error:
        _attach_service(_snapshot()).attach(1)

    assert error.value.status_code == 409
    assert error.value.detail["error"]["code"] == "NO_RUN"


@pytest.mark.parametrize(
    "path",
    [
        "/tasks/{task_id}/messages",
        "/tasks/{task_id}/messages/{message_id}/edit",
        "/tasks/{task_id}/resume",
        "/tasks/{task_id}/decisions",
        "/tasks/{task_id}/cancel",
        "/tasks/{task_id}/tool-calls/{tool_call_id}/cancel",
    ],
)
def test_task_commands_are_addressed_by_task(path: str) -> None:
    """命令只对 Task 或消息寻址；统一入口 ``/assistant`` 与按 run 寻址的端点不复存在。"""

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert path in paths
    assert "/assistant" not in paths
    assert not any(p.startswith("/runs/") for p in paths)


def test_restart_recovery_cancels_run_and_closes_tool_calls() -> None:
    """重启恢复只写持久化事实（Run 终态 + 工具收口），不触碰 snapshot。"""

    calls: list[tuple[int, RunTransition, str | None]] = []
    invalidated: list[tuple[int, int]] = []
    repaired: list[tuple[int, int]] = []

    class _Transaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class _SessionFactory:
        def begin(self) -> _Transaction:
            return _Transaction()

    class _RunCrud:
        def list_recoverable(self) -> list[object]:
            return [
                SimpleNamespace(
                    id=run_id, task_id=task_id, execution_data=ConversationRunProgress()
                )
                for run_id, task_id in ((7, 1), (8, 2))
            ]

    class _RunStateService:
        def transition(
            self,
            run_id: int,
            transition: RunTransition,
            *,
            end_reason: str | None = None,
            execution_data: object = None,
            session: object = None,
        ) -> object:
            assert session is not None
            calls.append((run_id, transition, end_reason))
            return SimpleNamespace(id=run_id)

    class _ContextService:
        def invalidate_waiting_calls(self, task_id: int, run_id: int, session: object) -> None:
            assert session is not None
            invalidated.append((task_id, run_id))

        def close_unclosed_tool_calls_for_run(
            self, task_id: int, run_id: int, session: object = None
        ) -> list[str]:
            assert session is not None
            repaired.append((task_id, run_id))
            return [f"call-{run_id}"]

    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _RunCrud()
    service._run_state = _RunStateService()
    service._context = _ContextService()
    service._session_factory = _SessionFactory()

    result = service.recover_orphaned_runs()

    assert [record.id for record in result] == [7, 8]
    assert calls == [
        (7, RunTransition.CANCEL, "runtime_restarted"),
        (8, RunTransition.CANCEL, "runtime_restarted"),
    ]
    assert invalidated == [(1, 7), (2, 8)]
    assert repaired == [(1, 7), (2, 8)]


def test_state_route_has_one_read_method() -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/tasks/{task_id}/assistant/state"
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_state_endpoint_returns_nested_run_snapshot() -> None:
    snapshot = _snapshot()

    class _TaskService:
        def get_task(self, _task_id: int) -> object:
            return object()

    class _TransportService:
        def get_state(self, _task_id: int) -> dict[str, object]:
            return snapshot

    result = await assistant_transport_state(1, _TaskService(), _TransportService())

    assert result == snapshot
    assert set(result) == {
        "runs",
        "current_run_id",
        "approvals",
        "context_window_total",
        "error",
    }


async def test_task_gate_waits_without_blocking_event_loop(tmp_path: Path) -> None:
    """闸门排队必须发生在工作线程：等待期间事件循环仍要能调度其它协程。

    Task 闸门要跨越 ``WorkflowSession`` 内的 ``await``；若用同步
    ``operation()`` 在事件循环线程上抢锁，同一 task 的并发请求会把事件循环整体卡在
    ``acquire`` 上，而持锁者又必须回到事件循环才能释放闸门，等待者遂只能等满超时。
    本用例断言等待期间心跳协程仍在推进。
    """

    workspace_service = get_workspace_service()
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = workspace_service.create_workspace("gate-test", str(root))
    task = workspace_service.create_task(workspace.id, "gate-test")
    space = task_runtime_spaces.get_or_create(task.id)
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
        async with WorkflowSession._gate(task.id):
            # 必须由心跳推进到主动释放，不能依靠持锁线程的保底超时。
            assert ticks == 5
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        if beating is not None:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)
        task_runtime_spaces.unload(task.id)

    assert not holder.is_alive()

# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")
