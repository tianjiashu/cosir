"""任务域端点。

包含任务查询、事件、checkpoint 与取消端点。所有端点通过
模块级 ``@app.*`` 装饰器直接注册到 ``app.api.app.app`` 单例上。
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime
from app.core.runtime.runner import AgentRuntime


@app.get("/health")
async def get_health(runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """返回后端健康状态与当前模型配置摘要。

    参数:
        runtime: 通过依赖注入的运行时单例。

    返回:
        不含 secret 原文的健康状态字典。

    异常:
        无。

    副作用:
        无。
    """

    return runtime.backend_health()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """返回任务状态。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        已存储的任务状态。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return runtime.get_task(task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/events")
async def list_events(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回任务的运行时事件。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        该任务的有序事件列表。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return [event.to_dict() for event in runtime.list_events(task_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/turns")
async def list_turns(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回任务下的轮次列表。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        该任务下的有序轮次列表。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return [turn.to_dict() for turn in runtime.list_turns(task_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/checkpoints")
async def list_checkpoints(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回任务的 checkpoint。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        该任务的有序 checkpoint 列表。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return [checkpoint.to_dict() for checkpoint in runtime.list_checkpoints(task_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """将任务标记为已取消。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        更新后的任务状态。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        在运行时存储中更新任务状态。
    """

    try:
        task = runtime.cancel_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return task.to_dict()
