"""任务领域端点。

包含任务查询、事件与运行期缓存 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询、轮次列表与事件回放均依赖业务 service；删除前停止执行经
``task_runtime_spaces.stop``。
"""

import asyncio

from fastapi import Depends, HTTPException, Query

from app.api.schemas import (
    ContextMessageResponse,
    DeleteRunResponse,
    DeleteTaskResponse,
    ForkTaskRequest,
    TaskResponse,
)
from app.app import app
from app.assistant_transport.state.message_identity import turn_of
from app.models.errors.deletion_errors import (
    DeletionBusyError,
    ProvisionalTaskCleanupConflictError,
    RunDeletionConflictError,
)
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.service.conversation_run.conversation_task_context_service import (
    ConversationTaskContextService,
)
from app.service.depends import (
    get_conversation_task_context_service,
    get_task_service,
)
from app.task_runtime.service.task_service import TaskService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@app.get("/context-messages", response_model=list[ContextMessageResponse])
async def list_context_messages(
    task_id: int | None = Query(default=None, gt=0),
    run_id: int | None = Query(default=None, gt=0),
    context_service: ConversationTaskContextService = Depends(
        get_conversation_task_context_service
    ),
) -> list[ContextMessageResponse]:
    """按 Task 或 Run 读取全部 Agent 上下文消息及其时间戳。"""

    if task_id is None and run_id is None:
        raise HTTPException(status_code=422, detail="task_id or run_id is required")
    try:
        records = context_service.list_observable_messages(task_id=task_id, run_id=run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or run not found") from exc
    return [ContextMessageResponse.from_record(record) for record in records]


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """返回单个任务及其派生状态。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        指定任务的 ``TaskResponse``。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无（仅读取）。
    """

    try:
        task = task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return TaskResponse.from_record(task)


@app.post("/tasks/{task_id}/fork")
async def fork_task(
    task_id: int,
    payload: ForkTaskRequest,
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """从源 Task 的某条消息处创建一个新的 fork Task：复制到这条消息所在那一轮回复为止。"""

    try:
        turn = turn_of(payload.message_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "FORK_MESSAGE_ID_INVALID", "message": str(exc), "retryable": False},
        ) from exc
    try:
        target = await task_service.fork_task(
            task_id,
            user_message_id=turn.user_message_id,
            leading_run_id=turn.leading_run_id,
            source_message_id=payload.message_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or message not found") from exc
    except TaskForkConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return TaskResponse.from_record(target)


@app.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    creation_command_id: str | None = Query(default=None, alias="creationCommandId"),
) -> DeleteTaskResponse:
    """删除单个任务及其级联的轮次与运行时事件。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service（负责级联清理）。
        creation_command_id: 前端在首条命令未被接受时回收 provisional Task 的创建标记；
            提供时走受控清理，任务树此时尚无 Run，故不经停止执行。

    返回:
        删除结果 ``DeleteTaskResponse``。

    异常:
        HTTPException: 任务不存在时 404；删除闸门忙时 409（可重试）；创建标记不匹配或
            任务已被接受时 409（不可重试）。

    副作用:
        普通删除先停止任务树内的活动执行并等待释放，再级联删除该任务下的轮次、运行时事件与
        任务自身。
    """

    try:
        # 级联删除是同步 DB 操作（单 BEGIN IMMEDIATE 写锁事务），经 asyncio.to_thread
        # 移出 event loop，避免冻结其它 task 的 turn 调度。
        if isinstance(creation_command_id, str):
            await asyncio.to_thread(
                task_service.cleanup_provisional_task,
                task_id,
                creation_command_id,
            )
        else:
            task_ids = await asyncio.to_thread(task_service.collect_task_tree_ids, task_id)
            # 先停下整棵任务树的 actor（停止执行并等它落定），再进删除闸门。
            await task_runtime_spaces.stop(task_ids)
            await asyncio.to_thread(task_service.delete_task, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    except ProvisionalTaskCleanupConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": False},
        ) from exc
    return DeleteTaskResponse(task_id=task_id, deleted=True)


@app.delete("/tasks/{task_id}/runs/{run_id}")
async def delete_run(
    task_id: int,
    run_id: int,
    task_service: TaskService = Depends(get_task_service),
) -> DeleteRunResponse:
    """删除单个 run 及其会话历史与产物。

    允许删除 task 内任意位置的 run（含中间），不重排剩余上下文序号。级联清理该 run 的
    会话消息、命令、委派记录、终端 session 与 run 行。

    参数:
        task_id: 来自路由的任务标识。
        run_id: 来自路由的 Conversation Run 标识。
        task_service: 通过依赖注入的任务 service（负责级联清理）。

    返回:
        删除结果 ``DeleteRunResponse``。

    异常:
        HTTPException: 404——task 或 run 不存在/不匹配；409——task 内存在运行中的 run
            （不可删除）或删除闸门忙。

    副作用:
        级联删除该 run 及其关联数据；与 delete_task 一致经 asyncio.to_thread 移出 event loop。
    """

    try:
        await asyncio.to_thread(task_service.delete_run, task_id, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or run not found") from exc
    except RunDeletionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return DeleteRunResponse(task_id=task_id, run_id=run_id, deleted=True)
