"""每会话可写交互通道（WebSocket ``terminal-v2``）。

一个 xterm 实例对应一条连接：首帧 ``attach`` 后服务端先重放输出、再推送实时输出与尺寸 / 输入标记 /
退出帧；客户端可发送 ``input`` / ``resize``（交还交接走 HTTP 决定入口）。
连接建立时按 Task 归属取到会话 runtime，之后输入直接调用它（只做内存更新与非阻塞入队），输出由订阅的
``asyncio.Queue`` 推送，
全程没有线程池轮询。断开连接只移除订阅，不关闭会话、不取消 Run。
"""

import asyncio
import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import Depends, WebSocket, WebSocketDisconnect

from app.app import app
from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.depends import get_terminal_session_service
from app.tools.terminal_runtime.errors import (
    TerminalSessionError,
    TerminalSessionResyncRequiredError,
    TerminalWorkerBackpressureError,
)
from app.tools.terminal_runtime.session_runtime import TerminalSessionRuntime
from app.tools.terminal_runtime.session_service import TerminalSessionService
from app.tools.terminal_runtime.stream import (
    OVERFLOW_EVENT_TYPE,
    PROTOCOL,
    TerminalStreamSubscription,
)

_PROTOCOL_ERROR_CODE = "TERMINAL_STREAM_PROTOCOL_ERROR"


@app.websocket("/tasks/{task_id}/terminal/sessions/{session_id}/stream")
async def terminal_session_stream(
    websocket: WebSocket,
    task_id: int,
    session_id: str,
    terminal_service: TerminalSessionService = Depends(get_terminal_session_service),
    trace_id: str | None = None,
) -> None:
    """建立 terminal-v2 交互通道；协议错误发 ``protocol_error`` 后以 1008 关闭。"""

    await websocket.accept()
    runtime: TerminalSessionRuntime | None = None
    subscription: TerminalStreamSubscription | None = None
    log_data = {"task_id": task_id, "session_id": session_id, "trace_id": trace_id}
    try:
        after_seq = _parse_attach(await _receive_frame(websocket))
        try:
            runtime = terminal_service.require(session_id, task_id)
        except TerminalSessionError as exc:
            await _send_protocol_error(websocket, session_id, exc.code, _public_error(exc))
            return
        try:
            attachment = runtime.subscribe(after_seq, asyncio.get_running_loop())
        except TerminalSessionResyncRequiredError as exc:
            await websocket.send_json(runtime.resync_event(exc.after_seq))
            await websocket.close(code=1013, reason="terminal output resync required")
            return
        except TerminalSessionError as exc:
            await _send_protocol_error(websocket, session_id, exc.code, _public_error(exc))
            return
        subscription = attachment.subscription
        log.info(
            "terminal_stream_attached",
            extra={"msg": "终端交互通道已连接", "data": {**log_data, "after_seq": after_seq}},
        )
        await websocket.send_json(attachment.attached)
        for event in attachment.replay:
            await websocket.send_json(event)
        if attachment.terminal_event is not None:
            await websocket.send_json(attachment.terminal_event)
            await websocket.close(code=1000)
            return
        identity = {
            "protocol": PROTOCOL,
            "session_id": session_id,
            "generation": attachment.attached["generation"],
        }
        await _serve(websocket, subscription, runtime, session_id, identity)
    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError) as exc:  # json.JSONDecodeError 是 ValueError 子类
        log.info(
            "terminal_stream_protocol_error",
            extra={
                "msg": "终端交互通道控制帧非法",
                "data": {**log_data, "error": str(exc), "error_type": type(exc).__name__},
            },
        )
        await _send_protocol_error(websocket, session_id, _PROTOCOL_ERROR_CODE, str(exc))
    finally:
        if runtime is not None and subscription is not None:
            runtime.unsubscribe(subscription)
            subscription.close()
            log.info("terminal_stream_detached", extra={"msg": "终端交互通道断开", "data": log_data})


async def _serve(
    websocket: WebSocket,
    subscription: TerminalStreamSubscription,
    runtime: TerminalSessionRuntime,
    session_id: str,
    identity: dict[str, object],
) -> None:
    """并发运行推送与接收；任一方结束（退出、重同步、断开、协议错误）即收尾另一方。"""

    send_lock = asyncio.Lock()

    async def send(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def pump() -> None:
        while True:
            event = await subscription.get()
            if event["type"] == OVERFLOW_EVENT_TYPE:
                await send(runtime.resync_event(None))
                await websocket.close(code=1013, reason="terminal stream backpressure")
                return
            await send(event)
            if event["type"] == "exit":
                await websocket.close(code=1000)
                return

    async def receive() -> None:
        while True:
            frame = await _receive_frame(websocket)
            await _handle_client_frame(frame, runtime, identity, send)

    tasks = {asyncio.create_task(pump()), asyncio.create_task(receive())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()


async def _handle_client_frame(
    frame: dict[str, object],
    runtime: TerminalSessionRuntime,
    identity: dict[str, object],
    send: Callable[[dict[str, object]], Awaitable[None]],
) -> None:
    """客户端帧直接调用会话 runtime；领域状态冲突（如会话已结束时 resize）记录后忽略。"""

    kind = frame.get("type")
    try:
        if kind == "input":
            _require_keys(frame, {"data_base64"})
            data = _decode_input(frame["data_base64"])
            try:
                result = runtime.user_input(data)
            except TerminalWorkerBackpressureError:
                await send({**identity, "type": "input_rejected", "reason": "backpressure"})
                return
            if result == "session_ended":
                await send({**identity, "type": "input_rejected", "reason": "session_ended"})
        elif kind == "resize":
            _require_keys(frame, {"rows", "cols"})
            rows, cols = _bounded_int(frame["rows"], 2, 500), _bounded_int(frame["cols"], 10, 1000)
            runtime.resize(rows, cols)
        else:
            raise ValueError(f"unsupported terminal control frame: {kind!r}")
    except TerminalWorkerBackpressureError:
        log.warning(
            "terminal_stream_control_backpressure",
            extra={"msg": "终端控制帧遇到 worker 背压", "data": {"session_id": runtime.session_id}},
        )
    except TerminalSessionError as exc:
        log.info(
            "terminal_stream_control_ignored",
            extra={
                "msg": "终端控制帧因会话状态被忽略",
                "data": {"session_id": runtime.session_id, "frame": kind, "code": exc.code},
            },
        )


def _require_keys(frame: dict[str, object], keys: set[str]) -> None:
    if set(frame) != {"type", *keys}:
        raise ValueError(f"control frame {frame.get('type')!r} must contain exactly {sorted(keys)}")


def _decode_input(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("data_base64 must be a string")
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error as exc:
        raise ValueError("data_base64 is not valid base64") from exc
    if not data or len(data) > Constant.Terminal.MAX_RAW_INPUT_BYTES:
        raise ValueError("input must be 1 byte to 64 KiB")
    return data


def _bounded_int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"value must be an integer in {low}..{high}")
    return value


async def _receive_frame(websocket: WebSocket) -> dict[str, object]:
    """读取并解析一个受限大小的 JSON 文本控制帧。"""

    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    payload = message.get("text")
    if payload is None:
        payload_bytes = message.get("bytes")
        if not isinstance(payload_bytes, bytes):
            raise ValueError("control frame must be JSON text")
        payload = payload_bytes.decode("utf-8")
    if (
        not isinstance(payload, str)
        or len(payload.encode("utf-8")) > Constant.Terminal.MAX_STREAM_CONTROL_BYTES
    ):
        raise ValueError("control frame is too large")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("control frame must be an object")
    return cast(dict[str, object], value)


def _parse_attach(frame: dict[str, object]) -> int | None:
    """校验首帧 attach，并返回重连 cursor。"""

    if frame.get("type") != "attach" or set(frame) - {"type", "after_seq"}:
        raise ValueError("first control frame must be attach")
    value = frame.get("after_seq")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("after_seq must be a non-negative integer or null")
    return value


async def _send_protocol_error(
    websocket: WebSocket, session_id: str, code: str, message: str
) -> None:
    """尽力发送受控协议错误并关闭连接；连接已断开时静默结束。

    ``code`` 与 HTTP 错误体的 ``code`` 同源（领域错误的稳定码，控制帧非法为
    ``TERMINAL_STREAM_PROTOCOL_ERROR``），客户端按它分支；``message`` 只供日志排查。
    """

    try:
        await websocket.send_json(
            {
                "protocol": PROTOCOL,
                "session_id": session_id,
                "type": "protocol_error",
                "code": code,
                "message": message,
            }
        )
        await websocket.close(code=1008, reason="invalid terminal stream protocol")
    except (RuntimeError, WebSocketDisconnect):
        pass


def _public_error(exc: TerminalSessionError) -> str:
    """把领域错误转换为不泄漏路径 / 异常正文的 WS 错误。"""

    if exc.code == "TERMINAL_SESSION_NOT_FOUND":
        return "terminal session not found"
    if exc.code == "TERMINAL_SESSION_OWNERSHIP_ERROR":
        return "terminal session is not owned by this task"
    return "terminal stream unavailable"
