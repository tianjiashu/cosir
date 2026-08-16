"""任务领域端点。

包含任务查询、事件与 checkpoint 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询、轮次列表与事件回放均依赖业务 service；取消与健康检查属于
运行时执行 / 生命周期职责，仍依赖 ``AgentRuntime``。
"""

from fastapi import Depends, HTTPException

from app.api.dependencies import (
    get_runtime_event_service,
    get_task_service,
    get_turn_service,
)
from app.api.schemas import (
    DeleteTaskResponse,
    RuntimeEventResponse,
    TaskResponse,
    TurnResponse,
)
from app.app import app
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
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
        return TaskResponse.from_record(task_service.get_task(task_id))
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
    return [TurnResponse.from_record(turn) for turn in turns]


@app.get("/tasks/{task_id}/events")
async def replay_task_events(
    task_id: str,
    task_service: TaskService = Depends(get_task_service),
    event_service: RuntimeEventService = Depends(get_runtime_event_service),
) -> list[RuntimeEventResponse]:
    """回放某任务下的完整运行时事件流（按 turn + sequence 升序）。

    用于打开任务时一次性重建含思考 / 工具调用 / 状态变更的 timeline，支撑历史
    回看与刷新后重连重渲染；只读查询，不重新执行 Agent。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service（用于任务存在性守卫）。
        event_service: 通过依赖注入的运行时事件 service。

    返回:
        按 ``(turn_id, sequence)`` 升序排列的事件回放列表，无记录时返回空列表。

    异常:
        HTTPException: 当任务不存在时抛出（沿用任务级 404 守卫）。

    副作用:
        无（只读查询）。
    """

    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    events = event_service.list_by_task(task_id)
    return [RuntimeEventResponse.from_event_dict(e) for e in events]


@app.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
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
        task_service.delete_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return DeleteTaskResponse(task_id=task_id, deleted=True)


@app.get("/tasks/{task_id}/children")
async def list_child_tasks(
    task_id: str,
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


@app.get("/tasks/{task_id}/turns/{turn_id}/events")
async def replay_turn_events(
    task_id: str,
    turn_id: str,
    event_service: RuntimeEventService = Depends(get_runtime_event_service),
) -> list[RuntimeEventResponse]:
    """回放某轮次下的运行时事件流（按 sequence 升序）。

    用于单轮详情页 / 单轮重连重渲染；只读查询，不重新执行 Agent。该轮次需
    隶属于路由中的 ``task_id``（交由调用方保证一致性，本端点不重复校验归属）。

    参数:
        task_id: 来自路由的任务标识（仅作为 URL 层级语义锚点）。
        turn_id: 来自路由的轮次标识。
        event_service: 通过依赖注入的运行时事件 service。

    返回:
        按 ``sequence`` 升序排列的该轮次事件回放列表，无记录时返回空列表。

    异常:
        无。

    副作用:
        无（只读查询）。
    """

    events = event_service.list_by_turn(turn_id)
    return [RuntimeEventResponse.from_event_dict(e) for e in events]
