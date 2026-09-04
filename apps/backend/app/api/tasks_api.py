"""任务领域端点。

包含任务查询、事件与 checkpoint 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询、轮次列表与事件回放均依赖业务 service；取消与健康检查属于
运行时执行 / 生命周期职责，仍依赖 ``AgentRuntime``。
"""

import asyncio

from fastapi import Depends, HTTPException

from app.api.dependencies import get_conversation_run_service, get_task_service
from app.api.schemas import (
    DeleteTaskResponse,
    TaskResponse,
)
from app.app import app
from app.config.logging.logger import log
from app.service.provider.capability_service import CapabilityService
from app.service.task.conversation_run_service import ConversationRunService
from app.task_runtime.service.task_service import TaskService


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    conversation_run_state_service: ConversationRunService = Depends(get_conversation_run_service),
) -> TaskResponse:
    """返回任务状态（含生命周期 status、上下文窗口占用与派生 execution_status）。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        ``TaskResponse``：已存储的任务状态（含 ``status``/``execution_status``，
        以及 ``context_usage_used`` 与动态计算的 ``context_window_total``）。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        record = task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc

    # context_window_total 任务级口径（§6.4）：优先按该 task 最近一次 turn 的
    # model_name 计算；无 turn / 无 model_name 时回退 Agent 默认模型名计算。
    # 该值仅用于前端上下文窗口上限展示，解析失败不阻断任务返回。
    context_window_total = None
    try:
        target_model = _latest_conversation_run_model_name(task_id, conversation_run_state_service)
        if target_model is not None:
            context_window_total = CapabilityService.get_model_context_window(target_model)
    except Exception as exc:
        log.warning(
            "task_context_window_resolve_failed",
            extra={
                "msg": f"按最近 turn model_name 计算 context_window_total 失败，回退 None：{exc}",
                "data": {"task_id": task_id},
            },
        )
        context_window_total = None

    return TaskResponse.from_record(record, context_window_total=context_window_total)


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
    return DeleteTaskResponse(task_id=task_id, deleted=True)


@app.get("/tasks/{task_id}/children")
async def list_child_tasks(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
) -> list[TaskResponse]:
    """列出某任务下的全部子任务（委派子任务）。

    子任务不出现在工作区对话列表，但可通过父任务的该端点下钻查看其轨迹。

    参数:
        task_id: 来自路由的父任务标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        该父任务的直接子任务 ``TaskResponse`` 列表；无子任务时返回空列表。

    异常:
        HTTPException: 当父任务不存在（级联 KeyError）时抛出。

    副作用:
        无（只读查询）。
    """

    try:
        children = task_service.list_child_tasks(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return [TaskResponse.from_record(child) for child in children]


def _latest_conversation_run_model_name(
    task_id: int, conversation_run_state_service: ConversationRunService
) -> str | None:
    """取得某任务最近一次 turn 的 ``model_name``（设计 §6.4 任务级口径）。

    按创建时间升序取该 task 的全部 turn，返回最后一个非空 ``model_name``；无 turn
    或全部 turn 未落库模型名（历史 NULL / Auto 运行期尚未回写）时返回 None，由
    调用方回退到 Agent 默认模型名。

    参数:
        task_id: 任务标识。
        conversation_run_state_service: 轮次 service（只读查询）。

    返回:
        最近一次 turn 的 ``model_name``；无可用值时返回 None。

    异常:
        无（查询失败向上抛出，由调用方 try/except 收敛为 total 缺失）。

    副作用:
        无（只读查询）。
    """

    turns = conversation_run_state_service.list_runs_for_task(task_id)
    for turn in reversed(turns):
        if turn.model_name:
            return turn.model_name
    return None
