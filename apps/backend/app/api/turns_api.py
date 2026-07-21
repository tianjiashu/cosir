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

内部辅助
--------
``_sse_turn_events`` 负责把 ``runtime.run_turn`` 产出的事件迭代器转换为
SSE 文本帧；它不直接处理 HTTP，仅做格式适配。
"""

import json
import logging
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.app import app
from app.api.dependencies import get_runtime, get_turn_service
from app.api.schemas import CreateTurnRequest
from app.core.runtime.runner import AgentRuntime
from app.service.task.turn_service import TurnService


@app.post("/tasks/{task_id}/turns")
async def create_turn(
    task_id: str,
    payload: CreateTurnRequest,
    turn_service: TurnService = Depends(get_turn_service),
) -> dict:
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
        创建后的轮次状态字典，包含 ``turn_id``、所属 ``task_id``、状态与
        创建时间等字段，供客户端建立后续 SSE 流使用。

    异常:
        HTTPException: 当任务不存在（404）或输入为空/非法（400）时抛出。

    副作用:
        在存储中创建 turn 记录，但不启动运行、不产生运行时事件。
    """

    try:
        turn = turn_service.create_turn(task_id, payload.input_text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return turn.to_dict()


@app.get("/turns/{turn_id}/stream")
async def stream_turn(
    turn_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
    turn_service: TurnService = Depends(get_turn_service),
):
    """通过 SSE 流式返回轮次的运行时事件。

    仅对 ``pending`` 状态的轮次启动 Agent 运行并实时推送事件。非 pending
    轮次由调用方 409 守卫拒绝，历史回看请走 ``GET /tasks/{task_id}/turns``。
    本端点只负责「执行」，不承担回放或断线重连（断开即本轮结束，由 finally
    兜底标 failed）。

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

    返回:
        发送 ``text/event-stream`` 的 StreamingResponse，连接保持打开直到
        轮次运行结束或客户端断开（断开即本轮结束，由 finally 兜底标 failed）。

    异常:
        HTTPException: 当轮次不存在（404）或轮次非 pending（409，历史请走
            ``GET /tasks/{task_id}/turns``）时抛出。

    副作用:
        仅对 pending turn 调用 ``runtime.run_turn`` 启动运行；事件产出由
        ``_sse_turn_events`` 转换为 SSE 帧。不回放历史事件。
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
        _sse_turn_events(runtime, turn_id, turn_service, turn), media_type="text/event-stream"
    )


@app.post("/turns/{turn_id}/cancel")
async def cancel_turn(
    turn_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
    turn_service: TurnService = Depends(get_turn_service),
) -> dict:
    """取消指定轮次并中止其运行。

    置 turn 为 ``cancelled``，模型节点在下一轮循环检查到取消状态后停止派发工具，从而中止运行。

    参数:
        turn_id: 来自路由的轮次标识。
        runtime: 通过依赖注入的运行时（负责取消与状态推进）。
        turn_service: 通过依赖注入的轮次 service（用于校验 turn 存在）。

    返回:
        取消后的轮次状态字典。

    异常:
        HTTPException: 当轮次不存在（404）时抛出。

    副作用:
        置 turn 为 cancelled 并 emit 取消事件。
    """

    try:
        turn_service.get_turn(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="turn not found") from exc
    turn = runtime.cancel_turn(turn_id)
    return turn.to_dict()


async def _sse_turn_events(
    runtime: AgentRuntime,
    turn_id: str,
    turn_service: TurnService,
    turn: object | None = None,
) -> AsyncIterator[str]:
    """将轮次运行时事件转换为 SSE 传输格式字符串。

    参数:
        runtime: 产生轮次事件的运行时（执行引擎）。
        turn_id: 待运行的轮次标识（仅 pending 轮次会由 ``runtime.run_turn`` 实际执行）。
        turn_service: 轮次 service，用于在客户端断开时把孤儿轮落终态。
        turn: 可选，调用方已取出的轮次记录，透传给 ``runtime.run_turn``
            以避免重复查询存储。

    生成:
        SSE 格式的事件字符串。

    异常:
        KeyError: 当轮次在流式开始前消失时抛出。

    副作用:
        执行轮次事件（仅 pending 轮次由 ``runtime.run_turn`` 启动）；客户端断开且本轮仍在运行时，
        将轮次标记为 ``failed``（``end_reason="client_disconnected"``），避免孤儿 ``running``。
    """

    try:
        async for event in runtime.run_turn(turn_id, turn=turn):
            yield f"event: {event.event_type}\ndata: {json.dumps(event.to_dict())}\n\n"
    finally:
        # 客户端断开：若本轮仍在运行，说明 run 已随连接中止，标记断开避免孤儿 running。
        # 窄异常保护：轮次可能已被清理，避免 teardown 抛异常掩盖主流程结果。
        try:
            if turn_service.has_turn_status(turn_id, "running"):
                turn_service.update_turn_status(
                    turn_id, "failed", end_reason="client_disconnected"
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "turn_disconnect_mark_failed_failed",
                extra={
                    "msg": "failed to mark disconnected turn as failed",
                    "data": {"turn_id": turn_id},
                },
            )
