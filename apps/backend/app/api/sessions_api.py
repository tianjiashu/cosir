"""会话领域端点。

包含会话查询、事件与运行期缓存 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：会话查询、轮次列表与事件回放均依赖业务 service；删除（停止加删除记录）经
``SessionService.delete_session_tree``。
"""

import asyncio

from fastapi import Depends, HTTPException, Query

from app.api.schemas import (
    ContextMessageResponse,
    ContextObservationResponse,
    ContextTurnRunResponse,
    DeleteSessionResponse,
    ForkSessionRequest,
    SessionResponse,
)
from app.app import app
from app.models.errors.deletion_errors import (
    DeletionBusyError,
    ProvisionalSessionCleanupConflictError,
)
from app.models.errors.session_fork_errors import SessionForkConflictError
from app.service.conversation.session_context_service import (
    SessionContextService,
)
from app.service.depends import (
    get_session_context_service,
    get_session_service,
)
from app.service.session.session_service import SessionService


@app.get("/context-messages", response_model=ContextObservationResponse)
async def list_context_messages(
    session_id: int = Query(gt=0),
    turn: int | None = Query(default=None, gt=0),
    context_service: SessionContextService = Depends(
        get_session_context_service
    ),
) -> ContextObservationResponse:
    """读取 Session 的全部 Agent 上下文消息，或只读一个回合（附它所属 Run 的状态事实）。"""

    try:
        records, run = context_service.list_observable_messages(session_id, turn=turn)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return ContextObservationResponse(
        messages=[ContextMessageResponse.from_record(record) for record in records],
        run=ContextTurnRunResponse.from_record(run) if run is not None else None,
    )


@app.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    session_service: SessionService = Depends(get_session_service),
) -> SessionResponse:
    """返回单个会话及其派生状态。

    参数:
        session_id: 来自路由的会话标识。
        session_service: 通过依赖注入的会话 service。

    返回:
        指定会话的 ``SessionResponse``。

    异常:
        HTTPException: 当会话不存在时抛出。

    副作用:
        无（仅读取）。
    """

    try:
        session = session_service.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return SessionResponse.from_record(session)


@app.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: int,
    payload: ForkSessionRequest,
    session_service: SessionService = Depends(get_session_service),
) -> SessionResponse:
    """从源 Session 的某条回复往后分叉：新 Session 继承到这条回复为止的上下文。"""

    try:
        target = await session_service.fork_session(session_id, turn=payload.turn)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session or message not found") from exc
    except SessionForkConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from exc
    return SessionResponse.from_record(target)


@app.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    session_service: SessionService = Depends(get_session_service),
    creation_command_id: str | None = Query(default=None, alias="creationCommandId"),
    with_forks: bool = Query(default=False, alias="withForks"),
) -> DeleteSessionResponse:
    """删除单个会话及其级联的轮次与运行时事件。

    参数:
        session_id: 来自路由的会话标识。
        session_service: 通过依赖注入的会话 service（负责级联清理）。
        creation_command_id: 前端在首条命令未被接受时回收 provisional Session 的创建标记；
            提供时走受控清理，会话树此时尚无 Run，故不经停止执行。
        with_forks: 为真时连同全部 fork 后代一起删除；默认只删这一个，它的 fork 改挂到上一级。

    返回:
        删除结果 ``DeleteSessionResponse``。

    异常:
        HTTPException: 会话不存在时 404；删除闸门忙时 409（可重试）；创建标记不匹配或
            会话已被接受时 409（不可重试）。

    副作用:
        普通删除经 ``SessionService.delete_session_tree``：先停止所有权子树的活动执行并等待释放，再级联
        删除；它的 fork 默认不删除、改挂到上一级，``withForks`` 时一并删除。
    """

    try:
        # 级联删除是同步 DB 操作（单 BEGIN IMMEDIATE 写锁事务），经 asyncio.to_thread
        # 移出 event loop，避免冻结其它 session 的 turn 调度。
        if isinstance(creation_command_id, str):
            await asyncio.to_thread(
                session_service.cleanup_provisional_session,
                session_id,
                creation_command_id,
            )
            deleted_session_ids = frozenset({session_id})
        else:
            deleted_session_ids = await session_service.delete_session_tree(session_id, with_forks=with_forks)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    except ProvisionalSessionCleanupConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": False},
        ) from exc
    return DeleteSessionResponse(
        session_id=session_id, deleted=True, deleted_session_ids=sorted(deleted_session_ids)
    )
