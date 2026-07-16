"""任务域端点。

包含任务创建、查询、事件、checkpoint、SSE 流式与取消端点。所有端点通过
模块级 ``@app.*`` 装饰器直接注册到 ``app.api.app.app`` 单例上。
"""
import json
from typing import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.app import app
from app.api.dependencies import get_runtime
from app.api.schemas import CreateTaskRequest
from app.core.runtime.runner import AgentRuntime


@app.post("/tasks")
async def create_task(payload: CreateTaskRequest, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """根据纯文本输入创建任务。

    参数:
        payload: 包含 text 与可选 session_id 的请求体。
        runtime: 通过依赖注入的运行时单例。

    返回:
        创建后的任务状态。

    异常:
        HTTPException: 当输入文本缺失或为空白时抛出。

    副作用:
        在运行时存储中创建任务状态。
    """

    try:
        task = runtime.create_task(
            input_text=payload.text,
            session_id=payload.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task.to_dict()


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


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, runtime: AgentRuntime = Depends(get_runtime)):
    """通过 SSE 流式返回任务的运行时事件。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        发送 Server-Sent Events 的 StreamingResponse。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        运行任务，并在流式推送事件期间修改任务状态。
    """

    try:
        runtime.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc

    return StreamingResponse(
        _sse_events(runtime, task_id),
        media_type="text/event-stream",
    )


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


async def _sse_events(runtime: AgentRuntime, task_id: str) -> AsyncIterator[str]:
    """将运行时事件转换为 SSE 传输格式字符串。

    参数:
        runtime: 产生任务事件的运行时。
        task_id: 待运行的任务标识。

    生成:
        SSE 格式的事件字符串。

    异常:
        KeyError: 当任务在流式开始前消失时抛出。

    副作用:
        执行运行时任务并存储产生的事件。
    """

    async for event in runtime.run_task(task_id):
        yield f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"

