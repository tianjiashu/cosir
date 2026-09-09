"""任务领域端点。

包含任务查询、事件与 checkpoint 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询、轮次列表与事件回放均依赖业务 service；取消与健康检查属于
运行时执行 / 生命周期职责，仍依赖 ``AgentRuntime``。
"""

import asyncio

from fastapi import Depends, HTTPException

from app.api.schemas import (
    DeleteTaskResponse,
    ForkTaskRequest,
    TaskResponse,
)
from app.app import app
from app.models.errors.deletion_errors import DeletionBusyError
from app.models.errors.task_fork_errors import SnapshotNotReadyError, TaskForkConflictError
from app.service.depends import get_task_service
from app.task_runtime.service.task_service import TaskService


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
    return TaskResponse.from_record(
        task,
        fork_available=task_service.is_fork_available(task_id),
    )


@app.post("/tasks/{task_id}/fork")
async def fork_task(
    task_id: int,
    payload: ForkTaskRequest,
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """从源 Task 指定历史 Run 创建一个新的 fork Task。"""

    try:
        target = await task_service.fork_task(task_id, payload.run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or run not found") from exc
    except SnapshotNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SNAPSHOT_NOT_READY",
                "message": str(exc),
                "retryable": True,
            },
        ) from exc
    except TaskForkConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return TaskResponse.from_record(target, fork_available=True)


@app.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
) -> DeleteTaskResponse:
    """删除单个任务及其级联的轮次与运行时事件。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service（负责级联清理）。

    返回:
        删除结果 ``DeleteTaskResponse``。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        级联删除该任务下的轮次、运行时事件与任务自身。
    """

    try:
        # 级联删除是同步 DB 操作（单 BEGIN IMMEDIATE 写锁事务），经 asyncio.to_thread
        # 移出 event loop，避免冻结其它 task 的 turn 调度。
        await asyncio.to_thread(task_service.delete_task, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return DeleteTaskResponse(task_id=task_id, deleted=True)
