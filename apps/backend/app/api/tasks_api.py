"""任务领域端点。

包含任务查询、事件与 checkpoint 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询与轮次列表直接依赖 ``TaskService``；事件回放、取消与健康检查
属于运行时执行 / 生命周期职责，仍依赖 ``AgentRuntime``。
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.depends.dependencies import get_task_service, get_turn_service
from app.api.schemas import TaskResponse, TurnResponse
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """返回任务状态（含生命周期 status 与派生 execution_status）。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        ``TaskResponse``：已存储的任务状态（含 ``status`` 与 ``execution_status``）。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return TaskResponse(**task_service.get_task(task_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/turns")
async def list_turns(
    task_id: str,
    turn_service: TurnService = Depends(get_turn_service),
) -> list[TurnResponse]:
    """列出某任务下的全部 turn（支撑多轮历史展示）。

    参数:
        task_id: 来自路由的任务标识。
        turn_service: 通过依赖注入的轮次 service。

    返回:
        该任务下按创建时间升序的 ``TurnResponse`` 列表。

    异常:
        HTTPException: 当任务不存在（级联 KeyError）时抛出。

    副作用:
        无。
    """

    try:
        turns = turn_service.list_turns_for_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return [TurnResponse(**turn.to_dict()) for turn in turns]
