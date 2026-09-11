"""只读 terminal preview WebSocket 路由。

本模块只开放 session 输出预览。Agent 对 session 的状态改变仍只能经隐藏的
terminal handler/service 调用；浏览器连接不能写入、signal 或 close PTY。
"""

import asyncio
import json
from typing import cast

from fastapi import Depends, WebSocket, WebSocketDisconnect

from app.app import app
from app.config.logging.logger import log
from app.service.depends import get_terminal_session_service
from app.service.terminal.errors import (
    TerminalSessionError,
    TerminalSessionResyncRequiredError,
)
from app.service.terminal.terminal_session_service import (
    TerminalPreviewSubscription,
    TerminalSessionService,
)

MAX_PREVIEW_CONTROL_BYTES = 8 * 1024
PREVIEW_POLL_SECONDS = 0.25


@app.websocket("/tasks/{task_id}/terminal/sessions/{session_id}/stream")
async def terminal_preview_stream(
    websocket: WebSocket,
    task_id: int,
    session_id: str,
    terminal_service: TerminalSessionService = Depends(get_terminal_session_service),
    trace_id: str | None = None,
) -> None:
    """建立只读 terminal preview 流，并拒绝所有状态改变消息。

    首帧必须是 ``{"type":"attach","after_seq": number|null}``。连接建立后，
    服务端先发送 attach replay，再发送实时 output/status/exit；客户端发来的
    非 attach 帧都被视为协议错误。断开连接只移除 subscriber，不关闭 session。
    """

    await websocket.accept()
    subscription: TerminalPreviewSubscription | None = None
    try:
        attach = await _receive_control_frame(websocket)
        after_seq = _parse_attach(attach)
        try:
            attachment = terminal_service.subscribe(
                session_id,
                task_id=task_id,
                after_seq=after_seq,
            )
        except TerminalSessionResyncRequiredError as exc:
            await websocket.send_json(
                _resync_event(
                    generation=None,
                    after_seq=exc.after_seq,
                    first_available_seq=exc.first_available_seq,
                    next_seq=exc.next_seq,
                )
            )
            await websocket.close(code=1013, reason="terminal output resync required")
            return
        except TerminalSessionError as exc:
            await _send_protocol_error(websocket, _public_error(exc))
            return

        subscription = attachment.subscription
        await websocket.send_json(attachment.attached)
        for event in attachment.replay:
            await websocket.send_json(event)
        if attachment.terminal_event is not None:
            await websocket.send_json(attachment.terminal_event)
            await websocket.close(code=1000)
            return
        subscription.activate()
        await _stream_events(websocket, subscription, terminal_service, session_id, task_id)
    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        log.info(
            "terminal_preview_protocol_error",
            extra={
                "msg": "Terminal preview 控制帧非法",
                "data": {
                    "task_id": task_id,
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            },
        )
        await _send_protocol_error(websocket, "invalid terminal preview control frame")
    finally:
        if subscription is not None:
            terminal_service.unsubscribe(session_id, subscription)


async def _stream_events(
    websocket: WebSocket,
    subscription: TerminalPreviewSubscription,
    terminal_service: TerminalSessionService,
    session_id: str,
    task_id: int,
) -> None:
    """并发消费 subscriber 和客户端控制帧。"""

    receive_task = asyncio.create_task(_receive_control_frame(websocket))
    try:
        while True:
            event_task = asyncio.create_task(
                asyncio.to_thread(subscription.get, PREVIEW_POLL_SECONDS)
            )
            done, _ = await asyncio.wait(
                {receive_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                receive_task.result()
                await _send_protocol_error(websocket, "only the initial attach frame is allowed")
                return
            event = event_task.result()
            if event is not None:
                if event.get("type") == "subscriber_overflow":
                    snapshot = terminal_service.snapshot(session_id, task_id=task_id)
                    await websocket.send_json(
                        _resync_event(
                            generation=snapshot.get("generation"),
                            after_seq=None,
                            first_available_seq=cast(int, snapshot["first_available_seq"]),
                            next_seq=cast(int, snapshot["next_seq"]),
                        )
                    )
                    await websocket.close(code=1013, reason="terminal preview backpressure")
                    return
                await websocket.send_json(event)
                if event.get("type") == "exit":
                    await websocket.close(code=1000)
                    return
            if receive_task.done():
                receive_task.result()
                await _send_protocol_error(websocket, "only the initial attach frame is allowed")
                return
    finally:
        receive_task.cancel()
        await asyncio.gather(receive_task, return_exceptions=True)


async def _receive_control_frame(websocket: WebSocket) -> dict[str, object]:
    """读取并解析一个受限大小的 JSON 文本控制帧。"""

    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    payload = message.get("text")
    if payload is None:
        payload_bytes = message.get("bytes")
        if not isinstance(payload_bytes, bytes):
            raise ValueError("control frame must be JSON text")
        if len(payload_bytes) > MAX_PREVIEW_CONTROL_BYTES:
            raise ValueError("control frame is too large")
        payload = payload_bytes.decode("utf-8")
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PREVIEW_CONTROL_BYTES:
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


async def _send_protocol_error(websocket: WebSocket, message: str) -> None:
    """尽力发送受控协议错误并关闭连接。"""

    try:
        await websocket.send_json(
            {"protocol": "terminal-preview-v1", "type": "protocol_error", "message": message}
        )
        await websocket.close(code=1008, reason="invalid terminal preview protocol")
    except RuntimeError:
        pass


def _public_error(exc: TerminalSessionError) -> str:
    """把领域错误转换为不泄漏路径/异常正文的 WS 错误。"""

    if isinstance(exc, TerminalSessionResyncRequiredError):
        return "terminal output resync required"
    if exc.code == "TERMINAL_SESSION_NOT_FOUND":
        return "terminal session not found"
    if exc.code == "TERMINAL_SESSION_OWNERSHIP_ERROR":
        return "terminal session is not owned by this task"
    return "terminal preview unavailable"


def _resync_event(
    *,
    generation: object,
    after_seq: int | None,
    first_available_seq: int,
    next_seq: int,
) -> dict[str, object]:
    """构造 cursor gap 事件。"""

    return {
        "protocol": "terminal-preview-v1",
        "type": "resync_required",
        "generation": generation,
        "after_seq": after_seq,
        "first_available_seq": first_available_seq,
        "next_seq": next_seq,
    }
