"""宿主工具链路的连接持有者：一条宿主 WebSocket 连接上的请求 / 应答配对。

帧格式（JSON）：
- 后端 → 宿主请求：``{"id": int, "method": str, "params": object, "meta": object}``
- 后端 → 宿主通知（不等应答）：``{"method": str, "params": object}``；取消即通知
  ``{"method": "$/cancel", "params": {"id": int}}``
- 宿主 → 后端应答：``{"id": int, "result": any}`` 或
  ``{"id": int, "error": {"code": str, "message": str}}``

``meta`` 只服务两端日志按调用关联（``task_id`` / ``tool_call_id``），宿主方法不应依赖它
做业务判断。

同一时刻只保留最新一条连接：宿主随后端 generation 重连时，新连接顶替旧连接，旧连接上的未决
请求以 ``UNAVAILABLE`` 失败。连接断开同理。不在链路内重放请求：失败交给工具决定是否重试。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from fastapi import WebSocket

from app.config.logging.logger import log
from app.tools.host.errors import CANCELLED, UNAVAILABLE, HostCallError

HOST_TOKEN_ENV = "CODING_AGENT_HOST_TOKEN"  # noqa: S105 环境变量名，不是凭据
# 被新连接顶替时关闭旧连接的 WebSocket 关闭码（4000–4999 为应用自定义）。
CLOSE_REPLACED = 4001
_CANCEL_METHOD = "$/cancel"


class _HostConnection:
    """一条已认证的宿主连接；pending 表只在连接所在事件循环上读写。"""

    def __init__(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop, host: dict[str, Any]):
        self.websocket = websocket
        self.loop = loop
        self.host = host
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()

    async def request(self, method: str, params: dict[str, Any], meta: dict[str, Any]) -> Any:
        """发出请求并等应答；被取消（工具撤销）时补发 ``$/cancel`` 再上抛。"""

        request_id = next(self._ids)
        future: asyncio.Future[Any] = self.loop.create_future()
        self._pending[request_id] = future
        started = time.monotonic()
        error_code: str | None = None
        try:
            await self._send({"id": request_id, "method": method, "params": params, "meta": meta})
            return await future
        except asyncio.CancelledError:
            error_code = CANCELLED
            await self._notify_quietly(_CANCEL_METHOD, {"id": request_id})
            raise
        except HostCallError as exc:
            error_code = exc.code
            raise
        finally:
            self._pending.pop(request_id, None)
            log.info(
                "host_call_finished",
                extra={
                    "msg": "宿主调用结束",
                    "data": {
                        "method": method,
                        "request_id": request_id,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "error_code": error_code,
                        **meta,
                    },
                },
            )

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    def resolve(self, frame: object) -> None:
        """把一帧应答交给对应请求；迟到的应答（请求已超时或取消）丢弃。

        异常:
            ValueError: 帧不是合法应答（由端点按协议错误关闭连接）。
        """

        if not isinstance(frame, dict) or not isinstance(frame.get("id"), int):
            raise ValueError("host frame must be an object with an integer id")
        future = self._pending.get(frame["id"])
        if "error" in frame:
            error = frame["error"]
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ValueError("host error frame must carry a string code")
            outcome: BaseException | None = HostCallError(
                error["code"], str(error.get("message", ""))
            )
        elif "result" in frame:
            outcome = None
        else:
            raise ValueError("host frame must carry result or error")
        if future is None or future.done():
            return
        if outcome is None:
            future.set_result(frame["result"])
        else:
            future.set_exception(outcome)

    def fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HostCallError(UNAVAILABLE, message))
        self._pending.clear()

    async def retire(self) -> None:
        """被新连接顶替：先让未决请求失败，再关闭连接（随后这条连接的 ``serve`` 正常收尾）。"""

        self.fail_pending("the desktop host reconnected")
        try:
            await self.websocket.close(
                code=CLOSE_REPLACED, reason="replaced by a newer host connection"
            )
        except Exception as exc:
            log.info(
                "host_link_close_failed",
                extra={
                    "msg": "关闭被顶替的宿主连接失败",
                    "data": {"error_type": type(exc).__name__},
                },
            )

    async def _send(self, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(frame)

    async def _notify_quietly(self, method: str, params: dict[str, Any]) -> None:
        """取消通知尽力而为：连接已断时宿主那边的处理也已随连接结束。"""

        try:
            await self.notify(method, params)
        except Exception as exc:
            log.info(
                "host_cancel_notify_failed",
                extra={"msg": "宿主取消通知未送达", "data": {"error_type": type(exc).__name__}},
            )


class HostLink:
    """进程级单例（``app.service.depends.get_host_link``）：持有当前宿主连接。

    事件循环侧：端点经 :meth:`serve` 把一条已认证连接交进来并在这里读到它断开。
    线程侧：工具经 :meth:`submit` 把请求投到连接所在循环（只经 ``call_host`` 使用）；
    :meth:`notify` 可在任意线程调用。

    token 在构造时读取宿主注入的 ``CODING_AGENT_HOST_TOKEN``；没有注入（绕过宿主直跑后端）时拒绝一切
    连接，依赖宿主的工具随之返回「宿主不可用」。
    """

    def __init__(self) -> None:
        self._token = os.environ.get(HOST_TOKEN_ENV, "")
        self._lock = threading.Lock()
        self._connection: _HostConnection | None = None
        self._on_connected: Callable[[], None] | None = None

    def set_on_connected(self, listener: Callable[[], None] | None) -> None:
        """注入「连上一条新连接」的监听方（lifespan 装配，``None`` 解除）。

        在连接所在事件循环上、开始读应答之前同步回调，只应排出后台任务：它若要经链路发请求，应答要靠
        :meth:`serve` 读回。链路本身不认识任何能力，连上后要对账的能力（浏览器标签回收）经这里接入。
        """

        self._on_connected = listener

    def accepts(self, token: object) -> bool:
        return (
            bool(self._token)
            and isinstance(token, str)
            and secrets.compare_digest(token, self._token)
        )

    async def serve(self, websocket: WebSocket, host: dict[str, Any]) -> None:
        """在一条已认证连接上读应答直到断开；返回前让这条连接上的未决请求失败。

        异常:
            WebSocketDisconnect: 连接断开（端点吞掉）。
            ValueError / TypeError: 宿主发来非法帧（端点按协议错误关闭）。
        """

        connection = _HostConnection(websocket, asyncio.get_running_loop(), host)
        with self._lock:
            replaced, self._connection = self._connection, connection
        log.info("host_link_connected", extra={"msg": "宿主工具链路已连接", "data": {"host": host}})
        if replaced is not None:
            log.info(
                "host_link_replaced",
                extra={"msg": "宿主工具链路被新连接顶替", "data": {"host": replaced.host}},
            )
            # 旧连接的 pending 表与 WebSocket 属于它自己的事件循环，收尾必须投回那里执行；
            # 不等它完成，新连接立即可用。
            asyncio.run_coroutine_threadsafe(replaced.retire(), replaced.loop)
        if self._on_connected is not None:
            self._on_connected()
        try:
            while True:
                connection.resolve(await websocket.receive_json())
        finally:
            with self._lock:
                if self._connection is connection:
                    self._connection = None
            connection.fail_pending("the desktop host disconnected")
            log.info(
                "host_link_disconnected", extra={"msg": "宿主工具链路断开", "data": {"host": host}}
            )

    def submit(self, method: str, params: dict[str, Any], meta: dict[str, Any]) -> Future[Any]:
        """线程侧入口：把请求投到当前连接所在循环；未连接时返回已失败（``UNAVAILABLE``）的 future。

        不得在事件循环线程上等待返回的 future（会阻塞循环）。
        """

        with self._lock:
            connection = self._connection
        if connection is None:
            future: Future[Any] = Future()
            future.set_exception(HostCallError(UNAVAILABLE, "the desktop host is not connected"))
            return future
        return asyncio.run_coroutine_threadsafe(
            connection.request(method, params, meta), connection.loop
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """发后即忘的通知（如 Task 删除后让宿主回收资源）；未连接时记日志后返回。"""

        with self._lock:
            connection = self._connection
        if connection is None:
            log.info(
                "host_notify_skipped",
                extra={"msg": "宿主未连接，通知未发送", "data": {"method": method}},
            )
            return
        future = asyncio.run_coroutine_threadsafe(
            connection.notify(method, params), connection.loop
        )
        future.add_done_callback(lambda done: _log_notify_failure(method, done))


def _log_notify_failure(method: str, future: Future[None]) -> None:
    if future.cancelled() or future.exception() is None:
        return
    log.warning(
        "host_notify_failed",
        extra={
            "msg": "宿主通知发送失败",
            "data": {"method": method, "error_type": type(future.exception()).__name__},
        },
    )
