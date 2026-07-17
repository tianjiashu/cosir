"""轮次域端点。"""

import json
from typing import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.app import app
from app.api.dependencies import get_runtime
from app.api.schemas import CreateTurnRequest
from app.core.runtime.runner import AgentRuntime


@app.post("/tasks/{task_id}/turns")
async def create_turn(task_id: str, payload: CreateTurnRequest, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """为已有任务追加一个 pending 轮次。

    参数:
        task_id: 来自路由的任务标识。
        payload: 包含本轮输入的请求体。
        runtime: 通过依赖注入的运行时单例。

    返回:
        创建后的轮次状态。

    异常:
        HTTPException: 当任务不存在或输入非法时抛出。

    副作用:
        在运行时存储中创建 turn，但不启动运行。
    """

    try:
        turn = runtime.create_turn(task_id, payload.input_text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return turn.to_dict()


@app.get("/turns/{turn_id}/stream")
async def stream_turn(turn_id: str, runtime: AgentRuntime = Depends(get_runtime)):
    """通过 SSE 流式返回轮次的运行时事件。

    参数:
        turn_id: 来自路由的轮次标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        发送 Server-Sent Events 的 StreamingResponse。

    异常:
        HTTPException: 当轮次不存在时抛出。

    副作用:
        对 pending turn 启动运行；对非 pending turn 回放事件。
    """

    try:
        runtime.get_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="turn not found") from exc
    return StreamingResponse(_sse_turn_events(runtime, turn_id), media_type="text/event-stream")


async def _sse_turn_events(runtime: AgentRuntime, turn_id: str) -> AsyncIterator[str]:
    """将轮次运行时事件转换为 SSE 传输格式字符串。

    参数:
        runtime: 产生轮次事件的运行时。
        turn_id: 待运行或回放的轮次标识。

    生成:
        SSE 格式的事件字符串。

    异常:
        KeyError: 当轮次在流式开始前消失时抛出。

    副作用:
        执行或回放轮次事件。
    """

    async for event in runtime.run_turn(turn_id):
        yield f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"
