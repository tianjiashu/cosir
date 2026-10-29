"""终端工作台 HTTP 路由：可用 shell、活跃会话计数、用户新建与关闭会话。

会话的画面、输入、尺寸与租约走每会话 WebSocket（``terminal_stream_api``），会话列表走 Transport
snapshot；本模块只做 wire 映射与错误映射；shell 清单与活跃计数直接读 ``ShellResolver`` 与终端
服务，新建 / 关闭的编排在 ``TerminalWorkbenchService``。阻塞调用（探测 shell、启动
worker、清理进程树）放进工作线程，不占事件循环。

错误统一为 ``detail={code, message, retryable}``：Task / 会话不存在或不属于该 Task 404，容量、
状态与 Task 类型冲突 409，shell 不可用 400，worker 不可用 503。
"""

import asyncio
from typing import NoReturn

from fastapi import HTTPException

from app.api.schemas.request.CreateTerminalSessionRequest import CreateTerminalSessionRequest
from app.api.schemas.response.TerminalWorkbenchResponse import (
    CreateTerminalSessionResponse,
    TerminalActiveCountResponse,
    TerminalShellsResponse,
)
from app.app import app
from app.config.logging.logger import log
from app.service.depends import get_terminal_session_service
from app.service.terminal.terminal_workbench_service import (
    TerminalWorkbenchService,
    TerminalWorkbenchTaskTypeError,
)
from app.tools.terminal_runtime.errors import (
    ShellResolutionError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver

_service = TerminalWorkbenchService()


def _raise(status_code: int, code: str, message: str, *, retryable: bool) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
    )


def _raise_terminal_error(exc: TerminalSessionError, *, task_id: int, operation: str) -> NoReturn:
    """把终端领域错误映射为 HTTP 状态；未单列的领域错误一律按状态冲突（409）处理。"""

    if isinstance(exc, TerminalSessionNotFoundError | TerminalSessionOwnershipError):
        status_code = 404
    elif isinstance(exc, ShellResolutionError):
        status_code = 400
    elif isinstance(exc, TerminalWorkerUnavailableError):
        status_code = 503
    else:
        status_code = 409
    log.warning(
        "terminal_workbench_request_rejected",
        extra={
            "msg": "终端工作台请求被拒",
            "data": {
                "task_id": task_id,
                "operation": operation,
                "status_code": status_code,
                "code": exc.code,
                "error": str(exc)[:500],
            },
        },
    )
    _raise(status_code, exc.code, str(exc), retryable=exc.retryable)


@app.get("/terminal/shells", response_model=TerminalShellsResponse)
async def list_terminal_shells() -> TerminalShellsResponse:
    """列出当前宿主可用的 shell 与集成支持。"""

    shells = await asyncio.to_thread(ShellResolver.available)
    return TerminalShellsResponse(shells=shells)


@app.get("/terminal/sessions/active-count", response_model=TerminalActiveCountResponse)
async def count_active_terminal_sessions() -> TerminalActiveCountResponse:
    """全进程活跃会话数（不区分 Task）；桌面宿主退出前据此提醒。"""

    count = await asyncio.to_thread(get_terminal_session_service().active_session_count)
    return TerminalActiveCountResponse(count=count)


@app.post(
    "/tasks/{task_id}/terminal/sessions",
    response_model=CreateTerminalSessionResponse,
)
async def create_terminal_session(
    task_id: int,
    body: CreateTerminalSessionRequest,
) -> CreateTerminalSessionResponse:
    """用户为侧栏对话 Task 新建终端会话；会话摘要随 snapshot 推送。"""

    try:
        session_id = await asyncio.to_thread(
            _service.start_user_session, task_id, shell=body.shell
        )
    except KeyError:
        _raise(404, "TASK_NOT_FOUND", "task not found", retryable=False)
    except TerminalWorkbenchTaskTypeError as exc:
        _raise(409, exc.code, str(exc), retryable=exc.retryable)
    except TerminalSessionError as exc:
        _raise_terminal_error(exc, task_id=task_id, operation="start")
    return CreateTerminalSessionResponse(session_id=session_id)


@app.delete("/tasks/{task_id}/terminal/sessions/{session_id}")
async def close_terminal_session(task_id: int, session_id: str) -> dict[str, object]:
    """关闭本 Task 的一个终端会话；会话不存在或不属于该 Task 时 404。"""

    try:
        await asyncio.to_thread(_service.close_session, task_id, session_id)
    except TerminalSessionError as exc:
        _raise_terminal_error(exc, task_id=task_id, operation="close")
    return {}
