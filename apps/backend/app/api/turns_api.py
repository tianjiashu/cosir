"""轮次（Turn）域 HTTP 端点。

本模块承载「任务 / 轮次」模型中轮次一侧的对外接口，负责把一次用户输入
转化为可执行、可观测的 Agent 运行单元，并把运行过程以 Server-Sent Events
实时推回桌面客户端。所有端点通过模块级 ``@app.*`` 装饰器直接注册到
``app.api.app.app`` 单例上。

领域模型关系
------------
- ``Task``（任务）：用户发起的一个完整目标，是持久化与展示的顶层单元。
- ``Turn``（轮次）：隶属于某个 ``Task`` 的一次执行单元，对应
  「用户一轮输入 → Agent 运行 → 产出结果」的闭环。一个 ``Task`` 可包含
  多个有序 ``Turn``；每个 ``Turn`` 在创建时为 ``pending`` 状态，运行结束
  后落定为终态（如 ``completed`` / ``failed``）。

提供的端点
----------
- ``POST /tasks/{task_id}/turns``：为指定任务追加一个 ``pending`` 轮次，
  写入本轮用户输入文本。**只创建不启动**——真正的 Agent 运行由随后的
  ``GET /turns/{turn_id}/stream`` 触发，便于客户端先拿到 ``turn_id`` 再
  建立 SSE 连接，避免竞态。
- ``GET /turns/{turn_id}/stream``：以 SSE 流式返回该轮次的运行时事件。
  **只负责执行** ``pending`` 轮次（pending → 认领 → 实时流）；非 pending 轮次直接 409。
  历史回看与刷新后重连不属于本端点职责，由 ``GET /tasks/{task_id}/turns`` 提供
  完整历史对话（含 Agent 回复文本）。事件帧格式为
  ``event: <event_type>\\ndata: <json>\\n\\n``。

客户端协作流程
--------------
典型流程：先 ``POST .../turns`` 拿到 ``turn_id``，再用该 ``turn_id`` 建立
``/turns/{turn_id}/stream`` 的 SSE 订阅，逐条消费 ``RuntimeEvent``（如
``token`` / ``tool_call`` / ``step`` / ``status`` 等），驱动 UI 的流式
渲染、工具调用展示与状态推进。

职责边界
--------
本模块只做接入层工作：取参数、状态守卫（404/409）、把 ``TurnStreamService`` 产出的
裸 ``RuntimeEvent`` 迭代器格式化为 SSE 帧。事件流编排（订阅/认领 producer/驱动/
发布/断连兜底/纯订阅转发）已下沉到
``app.service.task.turn_stream_service.TurnStreamService``；本模块保留：

- ``_format_sse_event``：把单个 ``RuntimeEvent`` 序列化为共用 SSE 帧（传输格式）。
- ``_sse_frames``：把 service 产出的裸事件迭代器逐条格式化为 SSE 帧（两端点复用）。
- ``runtime.run_turn`` 作为 ``TurnRunner`` 注入：api 层持有 ``AgentRuntime``
  （api → core 合法），把它适配为 service 依赖的轮次执行器协议，维持
  core → service 单向依赖。
"""

import json
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_runtime,
    get_turn_service,
    get_turn_stream_service,
)
from app.api.schemas import CreateTurnRequest, TurnResponse
from app.app import app
from app.core.runtime.runner import AgentRuntime
from app.models.event.runtime_event import RuntimeEvent
from app.service.task.turn_service import TurnService
from app.service.task.turn_stream_service import TurnStreamService


@app.post("/tasks/{task_id}/turns")
async def create_turn(
    task_id: int,
    payload: CreateTurnRequest,
    turn_service: TurnService = Depends(get_turn_service),
) -> TurnResponse:
    """为已有任务追加一个 pending 轮次。

    该端点只负责把用户本轮输入持久化为一个新的、处于 ``pending`` 状态的
    ``Turn`` 记录，并立即返回其 ``turn_id``；**不会**触发 Agent 运行。
    真正的执行由客户端随后对 ``GET /turns/{turn_id}/stream`` 的订阅触发，
    这样客户端可以先拿到 ``turn_id`` 再建立 SSE 连接，避免竞态。

    参数:
        task_id: 来自路由的任务标识。
        payload: 包含本轮用户输入文本的请求体（``input_text``）。
        turn_service: 通过依赖注入的轮次 service。

    返回:
        创建后的 ``TurnResponse``，包含 ``turn_id``、所属 ``task_id``、状态与
        创建时间等字段，供客户端建立后续 SSE 流使用。

    异常:
        HTTPException: 当任务不存在（404）或输入为空/非法（400）时抛出。

    副作用:
        在存储中创建 turn 记录，但不启动运行、不产生运行时事件。
    """

    try:
        turn = turn_service.create_turn(
            task_id,
            payload.input_text,
            agent_id="main_agent",
            product_id=payload.product_id,
            model_id=payload.model_id,
            thinking=payload.thinking,
            reasoning_effort=payload.reasoning_effort,
            paths=payload.paths,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TurnResponse.from_record(turn)


@app.get("/turns/{turn_id}/stream")
async def stream_turn(
    turn_id: int,
    runtime: AgentRuntime = Depends(get_runtime),
    turn_service: TurnService = Depends(get_turn_service),
    stream_service: TurnStreamService = Depends(get_turn_stream_service),
):
    """通过 SSE 流式返回轮次的运行时事件。

    仅对 ``pending`` 状态的轮次启动 Agent 运行并实时推送事件。非 pending
    轮次由调用方 409 守卫拒绝，历史回看请走 ``GET /tasks/{task_id}/turns``。
    本端点只负责「执行」，不承担回放或断线重连（断开即本轮结束，由运行时
    ``run_turn`` 的断开兜底标 failed）。本次运行使用的 agent 在轮次创建时即已绑定
    （``turn.agent_id``），
    运行时按「turn 绑定 > task 默认」解析，无需本端点再传参。

    事件以标准 SSE 帧推送，每帧格式为::

        event: <event_type>
        data: <json 序列化后的 RuntimeEvent>

    其中 ``event_type`` 对应 ``RuntimeEvent`` 的种类（如 ``token``、
    ``tool_call``、``step``、``status`` 等），``data`` 为该事件的 JSON 负载。
    相邻事件以空行 ``\\n\\n`` 分隔。

    参数:
        turn_id: 来自路由的轮次标识。
        runtime: 通过依赖注入的运行时（仅用于执行 pending 轮次）。
        turn_service: 通过依赖注入的轮次 service（用于取轮次记录与状态守卫）。
        stream_service: 通过依赖注入的轮次事件流编排 service（订阅/认领/驱动/发布/兜底）。

    返回:
        发送 ``text/event-stream`` 的 StreamingResponse，连接保持打开直到
        轮次运行结束或客户端断开（断开即本轮结束，由运行时 ``run_turn`` 的断开兜底标 failed）。

    异常:
        HTTPException: 当轮次不存在（404）或轮次非 pending（409，历史请走
            ``GET /tasks/{task_id}/turns``）时抛出。

    副作用:
        仅对 pending turn 调用 ``runtime.run_turn`` 启动运行；事件产出由
        ``_sse_frames`` 转换为 SSE 帧。不回放历史事件。
    """

    try:
        turn = turn_service.get_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="turn not found") from exc
    if turn.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="turn is not pending; fetch history via GET /tasks/{task_id}/turns",
        )
    return StreamingResponse(
        _sse_frames(stream_service.stream_turn_events(runtime.run_turn, turn)),
        media_type="text/event-stream; charset=utf-8",
    )


@app.get("/turns/{turn_id}/events/stream")
async def subscribe_turn_events(
    turn_id: int,
    turn_service: TurnService = Depends(get_turn_service),
    stream_service: TurnStreamService = Depends(get_turn_stream_service),
):
    """订阅已运行委派子轮次的实时事件。

    参数:
        turn_id: 来自路由的子轮次标识。
        turn_service: 用于读取轮次并校验其执行状态的领域 service。
        stream_service: 通过依赖注入的轮次事件流编排 service（纯订阅转发）。

    返回:
        ``text/event-stream`` 响应，仅转发此后实时发布到该 turn 的事件。

    异常:
        HTTPException: turn 不存在时为 404；turn 已终态时为 409。

    副作用:
        请求消费期间注册事件订阅；不认领 producer、不调用运行时，也不在断开时更新 turn 状态。
    """

    try:
        turn = turn_service.get_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="turn not found") from exc
    if turn.status not in ("pending", "running"):
        raise HTTPException(status_code=409, detail="turn is terminal")
    return StreamingResponse(
        _sse_frames(stream_service.stream_subscribed_turn_events(turn_id)),
        media_type="text/event-stream; charset=utf-8",
    )


@app.post("/turns/{turn_id}/cancel")
async def cancel_turn(
    turn_id: int,
    runtime: AgentRuntime = Depends(get_runtime),
    turn_service: TurnService = Depends(get_turn_service),
) -> TurnResponse:
    """取消指定轮次并中止其运行。

    置 turn 为 ``cancelled``，模型节点在下一轮循环检查到取消状态后停止派发工具，从而中止运行。

    参数:
        turn_id: 来自路由的轮次标识。
        runtime: 通过依赖注入的运行时（负责取消与状态推进）。
        turn_service: 通过依赖注入的轮次 service（用于校验 turn 存在）。

    返回:
        取消后的 ``TurnResponse``。

    异常:
        HTTPException: 当轮次不存在（404）时抛出。

    副作用:
        置 turn 为 cancelled 并 emit 取消事件。
    """

    try:
        turn_service.get_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="turn not found") from exc
    try:
        turn = runtime.cancel_turn(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TurnResponse.from_record(turn)

async def _sse_frames(events: AsyncIterator[RuntimeEvent]) -> AsyncIterator[str]:
    """把 service 产出的裸运行时事件迭代器逐条格式化为 SSE 帧。

    ``TurnStreamService`` 只产出裸 ``RuntimeEvent``（业务编排不感知传输格式），
    SSE 帧格式化属于接入层职责，由本函数收口；``stream_turn`` 与
    ``subscribe_turn_events`` 两个端点共用，避免重复的格式化循环。

    参数:
        events: 由 ``TurnStreamService`` 产出的裸事件异步迭代器。

    生成:
        与 ``_format_sse_event`` 一致的 SSE 帧文本。

    异常:
        透传事件迭代器抛出的异常。

    副作用:
        无。
    """
    async for event in events:
        yield f"event: {event.event_type}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
