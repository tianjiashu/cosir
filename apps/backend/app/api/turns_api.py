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

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.app import app
from app.api.depends.dependencies import (
    get_runtime,
    get_runtime_event_bus,
    get_turn_service,
)
from app.api.schemas import CreateTurnRequest, TurnResponse
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.models import TurnRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.run_failed_payload import RunFailedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.service.task.turn_service import TurnService

_TERMINAL_EVENT_TYPES = {
    EventType.RUN_FINISHED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
}


@app.post("/tasks/{task_id}/turns")
async def create_turn(
        task_id: str,
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
        turn = turn_service.create_turn(task_id, payload.input_text, agent_id=payload.agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TurnResponse.from_record(turn)


@app.get("/turns/{turn_id}/stream")
async def stream_turn(
        turn_id: str,
        runtime: AgentRuntime = Depends(get_runtime),
        turn_service: TurnService = Depends(get_turn_service),
        event_bus: RuntimeEventBus = Depends(get_runtime_event_bus),
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

    返回:
        发送 ``text/event-stream`` 的 StreamingResponse，连接保持打开直到
        轮次运行结束或客户端断开（断开即本轮结束，由运行时 ``run_turn`` 的断开兜底标 failed）。

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
        _sse_turn_events(
            runtime,
            turn_id,
            turn,
            event_bus,
            turn_service=turn_service,
        ),
        media_type="text/event-stream; charset=utf-8",
    )


@app.post("/turns/{turn_id}/cancel")
async def cancel_turn(
        turn_id: str,
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


async def _sse_turn_events(
        runtime: AgentRuntime,
        turn_id: str,
        turn: TurnRecord | None = None,
        event_bus: RuntimeEventBus | None = None,
        turn_service: TurnService | None = None,
) -> AsyncIterator[str]:
    """将轮次运行时事件转换为 SSE 传输格式字符串。

    断开兜底（把孤儿 ``running`` 落定为 ``failed``）已下沉到 ``runtime.run_turn`` 内部，
    按「本连接是否成功认领本轮」精确判定，避免并发连接互相误标。本函数只负责：把事件
    翻译为 SSE 帧、记录流式生命周期日志，并在 finally 中**确定性关闭**底层运行生成器，
    从而在客户端断开时触发 ``run_turn`` 的断开兜底（而非依赖不确定的 GC 回收）。

    参数:
        runtime: 产生轮次事件的运行时（执行引擎）。
        turn_id: 待运行的轮次标识（仅 pending 轮次会由 ``runtime.run_turn`` 实际执行）。
        turn: 可选，调用方已取出的轮次记录，透传给 ``runtime.run_turn``
            以避免重复查询存储。

    生成:
        SSE 格式的事件字符串。

    异常:
        不向上抛出：``KeyError``（轮次在流式开始前消失）与其它未预期异常均在此记录并终止流，
        避免异常裸奔中断 HTTP 响应；轮次终态由 ``run_turn`` 兜底。

    副作用:
        执行轮次事件（仅 pending 轮次由 ``runtime.run_turn`` 启动）；在 finally 中关闭底层
        运行生成器，触发 ``run_turn`` 的断开兜底（仅当本连接成功认领且轮次仍 ``running`` 时
        标记 ``failed``），避免孤儿 ``running``。
    """

    log.info(
        "turn_stream_started",
        extra={
            "msg": f"开始流式推送轮次事件，turn_id={turn_id}",
            "data": {"turn_id": turn_id},
        },
    )
    if event_bus is None:
        raise ValueError("event_bus is None")

    subscription = event_bus.subscribe(turn_id)
    producer: asyncio.Task[None] | None = None
    if event_bus.claim_turn_producer(turn_id):
        producer = asyncio.create_task(
            _drive_runtime_turn(
                runtime,
                turn_id,
                turn,
                event_bus,
                turn_service=turn_service,
            )
        )
    else:
        log.info(
            "turn_stream_producer_already_running",
            extra={
                "msg": f"轮次 producer 已存在，本连接仅订阅事件，turn_id={turn_id}",
                "data": {"turn_id": turn_id},
            },
        )
    terminal_received = False
    try:
        async for event in subscription:
            yield f"event: {event.event_type}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            if event.event_type in _TERMINAL_EVENT_TYPES:
                terminal_received = True
                # RUN_FINISHED 之后 run_turn 还会发布 file_change_stable（成功路径的
                # 变更集增量通知）。若立即 break，这些事件会滞留在订阅队列无法送达
                # 前端。故等待 producer 结束（run_turn 完全 return、事件已入队、
                # close_turn 已写入关闭哨兵），再继续消费剩余事件至订阅关闭。
                if event.event_type == EventType.RUN_FINISHED and producer is not None:
                    await producer
                    continue
                # RUN_FAILED / RUN_CANCELLED 之后无后续事件，保持原立即结束语义。
                break
        log.info(
            "turn_stream_completed",
            extra={
                "msg": f"轮次事件流式推送完成，turn_id={turn_id}",
                "data": {"turn_id": turn_id},
            },
        )
    except KeyError:
        # run_turn 在流式开始前发现轮次消失（如已被清理），无法继续推送。
        log.exception(
            "turn_stream_aborted",
            extra={
                "msg": f"轮次在执行前消失，流式中止，turn_id={turn_id}",
                "data": {"turn_id": turn_id},
            },
        )
    except Exception:
        # 其它未预期异常：记录后终止流，避免异常裸奔中断响应；轮次终态由 run_turn 兜底。
        log.exception(
            "turn_stream_error",
            extra={
                "msg": f"轮次事件流式推送异常，turn_id={turn_id}",
                "data": {"turn_id": turn_id},
            },
        )
    finally:
        event_bus.unsubscribe(subscription)
        if producer is not None:
            if terminal_received and not producer.done():
                with suppress(Exception):
                    await producer
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer
            if producer.done():
                with suppress(asyncio.CancelledError):
                    try:
                        producer.result()
                    except Exception:
                        log.exception(
                            "turn_stream_producer_failed",
                            extra={
                                "msg": f"轮次事件生产任务异常，turn_id={turn_id}",
                                "data": {"turn_id": turn_id},
                            },
                        )


async def _drive_runtime_turn(
        runtime: AgentRuntime,
        turn_id: str,
        turn: TurnRecord | None,
        event_bus: RuntimeEventBus,
        turn_service: TurnService | None = None,
) -> None:
    """Drive ``run_turn`` as an event producer.

    CodeGraph 索引保活（原 prepare 阶段）已迁移到 ``USER_PROMPT_SUBMIT`` Hook，
    在 ``run_turn`` 内部、本轮认领后、RUN_STARTED 之前触发，本函数不再做任何前置准备
    （见 CodeGraphIndexPrepareHook）。本函数只负责：启动 ``run_turn`` 并消费其事件
    发布到 bus，结束时释放 producer 槽位。

    参数:
        runtime: 产生轮次事件的运行时。
        turn_id: 待运行的轮次标识。
        turn: 可选预取轮次记录。
        event_bus: 当前进程 runtime event 广播总线。
        turn_service: 轮次 service（run 未启动即断开的兜底落 failed 用）。

    返回:
        无。

    异常:
        向上透传 ``runtime.run_turn`` 的未预期异常，由持有 producer 的 SSE 层记录；
        被取消时 re-raise ``CancelledError``（兜底落 failed 后不吞）。

    副作用:
        消费 ``run_turn`` 事件发布到 bus；结束时释放 producer 槽位。
    """

    entered_run = False

    async def execute() -> None:
        """消费 ``run_turn`` 事件并发布到 bus（producer 主体）。"""
        nonlocal entered_run
        entered_run = True
        events = runtime.run_turn(turn_id, turn=turn)
        try:
            async for event in events:
                event_bus.publish(event)
        finally:
            try:
                await events.aclose()
                event_bus.close_turn(turn_id)
            finally:
                event_bus.release_turn_producer(turn_id)

    try:
        await execute()
    except asyncio.CancelledError:
        # 仅 run_turn 尚未启动（turn 仍 pending）时本连接断开需落 failed 兜底；
        # run 阶段断开由 run_turn 内部 fail_turn_if_running 兜底（§九.3/4）。
        if not entered_run and turn_service is not None and turn_id:
            try:
                # 方法偏离说明（§九.4）：fail_turn_if_running 的 WHERE status="running"
                # 原子约束对 pending 不生效（turn_crud），而本 producer 已 claim 独占
                # （无并发认领竞态），故用无条件 update_turn_status 强制落 failed。
                # 兜底后 emit run_failed 提供终态事件。
                turn_service.update_turn_status(turn_id, "failed", end_reason="client_disconnected")
                _emit_run_failed(turn, turn_id)
            except Exception:
                log.exception(
                    "turn_disconnect_failed",
                    extra={"msg": "run 未启动即断开落 failed 失败", "data": {"turn_id": turn_id}},
                )
        raise
    finally:
        # 最外层释放 producer 槽位。
        event_bus.release_turn_producer(turn_id)


def _emit_run_failed(turn: TurnRecord | None, turn_id: str) -> None:
    """prepare 阶段断开兜底时发布一条 run_failed 终态事件。

    与 ``runtime.run_turn`` 内部落 failed 时的终态事件同构，经 ``RuntimeEventService``
    落库并发布，避免留下无终态事件的孤儿 turn（方案二 §4.2.1 / §六 验收 5）。

    参数:
        turn: 预取轮次记录（task_id 来源；为 None 时用空串）。
        turn_id: 待落终态的轮次标识。

    返回:
        无。

    异常:
        RuntimeError: storage 未初始化时抛出（由调用方兜底捕获）。

    副作用:
        向 runtime_events 表写入 run_failed 事件并广播。
    """
    task_id = turn.task_id if turn is not None else ""
    event = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id=task_id,
        turn_id=turn_id,
        payload=RunFailedPayload(error="client_disconnected", status="failed"),
    )
    RuntimeEventService().save_and_publish(event)
