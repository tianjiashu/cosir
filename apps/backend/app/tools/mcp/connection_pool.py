"""Agent 调用 MCP 服务使用的进程级长连接池。

为什么要长连接：短连接每次调用都要拉起 stdio 子进程并完成握手（``npx`` 类服务冷启动以秒计），
而且有状态的服务（浏览器、数据库会话）在两次调用之间丢失全部状态，等于不可用。

为什么在独立事件循环线程上：SDK 会话基于 anyio task group，进入与退出必须在同一个 task，
所以每个服务由一个常驻 owner task 持有会话；外部服务卡住或刷屏也不该拖慢主循环上的
HTTP / SSE / 停止请求。工具 handler 在工作线程里经 :meth:`MCPConnectionPool.submit` 把协程投到
池的循环上，自行轮询取消与超时（见 ``mcp_tool``）。

事实源与失效规则：
- 服务配置文件是唯一事实源，池不接收配置变更通知：每次取连接都比对连接指纹，不一致即关闭
  旧连接重建；被禁用或撤销测试状态的服务不再被调用方选中，其连接由空闲回收关闭。
- 连接级失败（见 ``is_connection_failure``）或 owner task 已结束时丢弃连接，下一次调用重连；
  失败的那次调用不在池内重放——工具可能有副作用，是否重试交给模型。
- 工具目录随连接缓存，收到 ``tools/list_changed`` 通知或调用了目录里没有的工具时刷新。

线程模型：``_connections`` 等状态只在池的事件循环线程上读写；``_lock`` 只保护循环线程的
启动与关闭。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncIterator, Coroutine
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO, TypeVar

from mcp import ClientSession, types

from app.config.logging.logger import log
from app.service.configuration.mcp_configuration import (
    MCPServerConfig,
    server_connection_fingerprint,
)
from app.tools.mcp.client import (
    MCPToolNotFoundError,
    describe_exception,
    is_connection_failure,
    open_client_session,
)
from app.utils.path import system_cosir

T = TypeVar("T")

# 建立连接（stdio 拉起子进程 + initialize 握手）的上限；与配置界面连接测试的量级一致。
CONNECT_TIMEOUT_SECONDS = 30.0
# 连接空闲超过该时长即关闭：stdio 服务是常驻子进程，不能无限占用资源。
IDLE_TIMEOUT_SECONDS = 600.0
_REAP_INTERVAL_SECONDS = 60.0
# 关闭单个连接时等待 owner task 收尾的上限，超时即取消该 task。
_CLOSE_TIMEOUT_SECONDS = 5.0
# 每个 stdio 服务 stderr 日志的大小上限，超出时在下次建连前轮转为 ``.1.log``。
_STDERR_LOG_MAX_BYTES = 5 * 1024 * 1024


class MCPConnectionPool:
    """按服务名持有 MCP 长连接；协程方法只能在池的事件循环上运行（经 :meth:`submit` 提交）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._connections: dict[str, _Connection] = {}
        self._open_locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 线程侧入口
    # ------------------------------------------------------------------

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        """把协程投到池的事件循环上执行；首次调用时惰性启动循环线程。

        异常:
            RuntimeError: 连接池已关闭（后端退出中）。
        """

        with self._lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("MCP connection pool is closed")
            if self._loop is None:
                self._start_loop()
            assert self._loop is not None
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def close(self) -> None:
        """关闭全部连接（stdio 服务子进程随之结束）并停止循环线程；可重复调用。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(
                timeout=_CLOSE_TIMEOUT_SECONDS * 2
            )
        except Exception as exc:
            log.error(
                "mcp_pool_close_failed",
                extra={"msg": "MCP 连接池关闭连接失败", "data": {"error": describe_exception(exc)}},
            )
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_CLOSE_TIMEOUT_SECONDS)
        log.info("mcp_pool_closed", extra={"msg": "MCP 连接池已关闭"})

    def _start_loop(self) -> None:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            self._reaper = loop.create_task(self._reap_idle_forever())
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:
                loop.close()

        thread = threading.Thread(target=run, name="mcp-connection-pool", daemon=True)
        thread.start()
        ready.wait()
        self._loop, self._thread = loop, thread

    # ------------------------------------------------------------------
    # 循环侧操作（只在池的事件循环上运行）
    # ------------------------------------------------------------------

    async def list_tools(self, server: MCPServerConfig) -> list[types.Tool]:
        """返回服务的工具目录（优先用连接缓存）。"""

        async with self._use(server) as connection:
            return await connection.tools()

    async def call_tool(
        self,
        server: MCPServerConfig,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        """调用服务上的一个工具；目录里没有该工具时先刷新一次目录再判定。

        异常:
            MCPToolNotFoundError: 刷新后的目录里仍没有该工具。
        """

        async with self._use(server) as connection:
            if not await connection.has_tool(tool_name):
                raise MCPToolNotFoundError(server.name, tool_name)
            return await connection.session.call_tool(tool_name, arguments)

    @asynccontextmanager
    async def _use(self, server: MCPServerConfig) -> AsyncIterator[_Connection]:
        """取（或建立）连接并计入在途调用；连接级失败时丢弃连接，异常原样上浮。"""

        connection = await self._acquire(server)
        connection.in_flight += 1
        try:
            yield connection
        except Exception as exc:
            if is_connection_failure(exc) or not connection.alive:
                await self._discard(connection, reason="connection_failed", error=exc)
            raise
        finally:
            connection.in_flight -= 1
            connection.last_used = time.monotonic()

    async def _acquire(self, server: MCPServerConfig) -> _Connection:
        fingerprint = server_connection_fingerprint(server)
        lock = self._open_locks.setdefault(server.name, asyncio.Lock())
        async with lock:
            current = self._connections.get(server.name)
            if current is not None:
                if current.alive and current.fingerprint == fingerprint:
                    return current
                reason = "config_changed" if current.alive else "connection_lost"
                await self._discard(current, reason=reason)
            connection = _Connection(server, fingerprint)
            started = time.monotonic()
            await connection.open()
            self._connections[server.name] = connection
            log.info(
                "mcp_connection_opened",
                extra={
                    "msg": "MCP 服务长连接已建立",
                    "data": {
                        "server_name": server.name,
                        "transport": server.transport,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    },
                },
            )
            return connection

    async def _discard(
        self,
        connection: _Connection,
        *,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        """从池中移除并关闭连接；同一连接被并发丢弃时只关闭一次。"""

        if self._connections.get(connection.server.name) is connection:
            del self._connections[connection.server.name]
        if connection.closing:
            return
        log.info(
            "mcp_connection_discarded",
            extra={
                "msg": "MCP 服务长连接已丢弃",
                "data": {
                    "server_name": connection.server.name,
                    "reason": reason,
                    "error": describe_exception(error) if error is not None else None,
                },
            },
        )
        await connection.close()

    async def _reap_idle_forever(self) -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL_SECONDS)
            await self.reap_idle()

    async def reap_idle(self, idle_seconds: float = IDLE_TIMEOUT_SECONDS) -> None:
        """关闭空闲超时或已断开且没有在途调用的连接。"""

        now = time.monotonic()
        for connection in list(self._connections.values()):
            if connection.in_flight:
                continue
            if not connection.alive:
                await self._discard(connection, reason="connection_lost")
            elif now - connection.last_used >= idle_seconds:
                await self._discard(connection, reason="idle")

    async def _close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for connection in list(self._connections.values()):
            await self._discard(connection, reason="pool_closed")


class _Connection:
    """一个服务的长连接：owner task 持有会话上下文，直到被要求关闭或传输失败。"""

    def __init__(self, server: MCPServerConfig, fingerprint: str) -> None:
        self.server = server
        self.fingerprint = fingerprint
        self.in_flight = 0
        self.last_used = time.monotonic()
        self.closing = False
        self._session: ClientSession | None = None
        self._tools: list[types.Tool] | None = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._close_requested = asyncio.Event()
        self._owner: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self._session is not None and self._owner is not None and not self._owner.done()

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise ConnectionError(f"MCP connection to {self.server.name} is closed")
        return self._session

    async def open(self) -> None:
        """启动 owner task 并等待握手完成；失败或超时时关闭连接并上抛原因。"""

        self._owner = asyncio.get_running_loop().create_task(
            self._own(), name=f"mcp-connection:{self.server.name}"
        )
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                await asyncio.shield(self._ready)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self.closing = True
        self._close_requested.set()
        if self._owner is not None and not self._owner.done():
            await self._wait_owner()
        # 握手失败时 owner 会把原因写进 ``_ready``；open 已把它上抛过，这里标记为已取回，
        # 避免 asyncio 在垃圾回收时再报「异常未被取回」。
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()

    async def _wait_owner(self) -> None:
        assert self._owner is not None
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                await asyncio.shield(self._owner)
        except TimeoutError:
            self._owner.cancel()
            log.warning(
                "mcp_connection_close_timed_out",
                extra={
                    "msg": "MCP 服务连接未在时限内收尾，已强制取消",
                    "data": {"server_name": self.server.name},
                },
            )

    async def tools(self) -> list[types.Tool]:
        if self._tools is None:
            self._tools = (await self.session.list_tools()).tools
        return self._tools

    async def has_tool(self, tool_name: str) -> bool:
        if any(tool.name == tool_name for tool in await self.tools()):
            return True
        # 服务可能新增了工具却没发 list_changed 通知：刷新一次再判定。
        self._tools = None
        return any(tool.name == tool_name for tool in await self.tools())

    async def _own(self) -> None:
        errlog = _open_stderr_log(self.server) if self.server.transport == "stdio" else None
        try:
            async with open_client_session(
                self.server,
                errlog=errlog,
                message_handler=self._on_message,
            ) as session:
                self._session = session
                self._ready.set_result(None)
                await self._close_requested.wait()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            elif not self.closing:
                log.warning(
                    "mcp_connection_lost",
                    extra={
                        "msg": "MCP 服务长连接意外断开",
                        "data": {
                            "server_name": self.server.name,
                            "error": describe_exception(exc),
                        },
                    },
                )
        finally:
            self._session = None
            if not self._ready.done():
                self._ready.set_exception(
                    ConnectionError(f"MCP connection to {self.server.name} closed before ready")
                )
            if errlog is not None:
                errlog.close()

    async def _on_message(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            self._tools = None
        elif isinstance(message, Exception):
            log.warning(
                "mcp_transport_message_error",
                extra={
                    "msg": "MCP 服务消息流出现异常",
                    "data": {"server_name": self.server.name, "error": describe_exception(message)},
                },
            )


def _open_stderr_log(server: MCPServerConfig) -> TextIO | None:
    """打开该 stdio 服务的 stderr 日志（``<LOG_DIR>/mcp/<server>.stderr.log``），超限先轮转一次。

    日志是诊断旁路：打不开时记录并返回 ``None``（退回后端 stderr），不阻断建连。
    """

    directory: Path = system_cosir.LOG_DIR / "mcp"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", server.name)
    path = directory / f"{safe_name}.stderr.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _STDERR_LOG_MAX_BYTES:
            path.replace(directory / f"{safe_name}.stderr.1.log")
        return path.open("a", encoding="utf-8")
    except OSError as exc:
        log.warning(
            "mcp_stderr_log_unavailable",
            extra={
                "msg": "MCP 服务 stderr 日志无法打开，退回后端 stderr",
                "data": {"server_name": server.name, "error": describe_exception(exc)},
            },
        )
        return None
