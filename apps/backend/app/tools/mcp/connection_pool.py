"""Agent 调用 MCP 服务使用的进程级长连接池。

为什么要长连接：短连接每次调用都要拉起 stdio 子进程并完成握手（``npx`` 类服务冷启动以秒计），
而且有状态的服务（浏览器、数据库会话）在两次调用之间丢失全部状态，等于不可用。

为什么在独立事件循环线程上：SDK 会话基于 anyio task group，进入与退出必须在同一个 task，
所以每个服务由一个常驻 owner task 持有会话；外部服务卡住或刷屏也不该拖慢主循环上的
HTTP / SSE / 停止请求。工具 handler 在工作线程里经 :meth:`MCPConnectionPool.submit` 把协程投到
池的循环上，自行轮询取消与超时（见 ``mcp_tools``）。

事实源与失效规则：
- 服务配置文件是唯一事实源，池不接收配置变更通知：每次取连接都比对连接指纹，不一致即关闭
  旧连接重建；被禁用或撤销测试状态的服务不再被调用方选中，其连接由空闲回收关闭。
- 连接级失败（见 ``is_connection_failure``）或 owner task 已结束时丢弃连接，下一次调用重连；
  失败的那次调用不在池内重放——工具可能有副作用，是否重试交给模型。
- owner task 先结束时，在途请求带着它结束的原因立刻失败（:meth:`_Connection.request`）。会话的请求在别的 task
  里等响应，传输层出错（长连接中途授权失效、服务端回 500）时 SDK 以取消的方式拆掉会话，给在途请求回
  「连接已关闭」的那一步也一并被取消，不在这里收口，调用会一直挂到工具时限，原因也丢了。
- 工具目录随连接缓存，收到 ``tools/list_changed`` 通知或调用了目录里没有的工具时刷新；每次列到的目录（连同
  握手时的服务说明）与本机记录不同就回写（``MCPMachineState.record_catalog``），Agent 的延迟工具目录与服务
  说明只读那份记录、不连接服务。

线程模型：``_connections`` 等状态只在池的事件循环线程上读写；``_lock`` 只保护循环线程的
启动与关闭。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO, TypeVar

from mcp import ClientSession, types

from app.config.logging.logger import log
from app.core.tool.validation.arguments import validate_tool_arguments
from app.service.configuration.mcp.configuration import ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.tools.mcp.client import (
    MCPToolArgumentsError,
    MCPToolNotFoundError,
    describe_exception,
    is_connection_failure,
    open_client_session,
    server_catalog,
)
from app.utils.path import system_cosir

T = TypeVar("T")

# 建立连接（stdio 拉起子进程 + initialize 握手）的上限；与配置界面连接测试的量级一致。
CONNECT_TIMEOUT_SECONDS = 30.0
# 连上之后一次 ``tools/list`` 的上限：只管请求本身，不含建连（冷启动慢的服务不该因此永远列不出目录）。
LIST_TOOLS_TIMEOUT_SECONDS = 20.0
# 连接空闲超过该时长即关闭：stdio 服务是常驻子进程，不能无限占用资源。
IDLE_TIMEOUT_SECONDS = 600.0
_REAP_INTERVAL_SECONDS = 60.0
# 关闭单个连接时等待 owner task 收尾的上限，超时即取消该 task。
_CLOSE_TIMEOUT_SECONDS = 5.0
# 每个 stdio 服务 stderr 日志的大小上限，超出时在下次建连前轮转为 ``.1.log``。
_STDERR_LOG_MAX_BYTES = 5 * 1024 * 1024


class MCPConnectionPool:
    """按连接身份（``ScopedMCPServer.pool_key``：定义作用域 + 使用的工作区 + 服务名）持有 MCP 长连接。

    协程方法只能在池的事件循环上运行（经 :meth:`submit` 提交）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._connections: dict[tuple[str, str, str], _Connection] = {}
        self._open_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
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

    async def list_tools(self, server: ScopedMCPServer) -> list[types.Tool]:
        """返回服务的工具目录（优先用连接缓存）。

        建连与列目录分别限时：建连归 ``CONNECT_TIMEOUT_SECONDS``，列目录只限连上之后的请求。列目录超时抛
        ``TimeoutError``，它是 ``OSError`` 的子类，``_use`` 会把它当连接级失败丢弃连接：连上了却 20 秒不回
        目录的服务，下次重连是合理的。
        """

        async with self._use(server) as connection:
            async with asyncio.timeout(LIST_TOOLS_TIMEOUT_SECONDS):
                return await connection.tools()

    async def call_tool(
        self,
        server: ScopedMCPServer,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        """调用服务上的一个工具：先确认工具存在（目录里没有时刷新一次），再按它声明的输入 schema
        校验参数，合格才发给服务。

        校验用连接上缓存的目录，不多发请求；参数不合格时请求不出本机，报错带上 schema 让模型照着改。
        服务声明的 schema 本身不合法时不拦调用（那是服务的问题，拦下会让它的所有工具都用不了），
        只记日志，交给服务自己判断参数。

        异常:
            MCPToolNotFoundError: 刷新后的目录里仍没有该工具。
            MCPToolArgumentsError: 参数不符合工具声明的输入 schema。
        """

        async with self._use(server) as connection:
            tool = await connection.find_tool(tool_name)
            if tool is None:
                raise MCPToolNotFoundError(server.name, tool_name)
            validation = validate_tool_arguments(arguments, tool.inputSchema)
            if not validation.ok and not validation.schema_invalid:
                raise MCPToolArgumentsError(server.name, tool_name, validation.error, tool.inputSchema)
            if validation.schema_invalid:
                log.warning(
                    "mcp_tool_schema_invalid",
                    extra={
                        "msg": "MCP 工具声明的输入 schema 不合法，跳过本地参数校验",
                        "data": {
                            "server_name": server.name,
                            "tool_name": tool_name,
                            "error": validation.error,
                        },
                    },
                )
            return await connection.call_tool(tool_name, arguments)

    @asynccontextmanager
    async def _use(self, server: ScopedMCPServer) -> AsyncIterator[_Connection]:
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

    async def _acquire(self, server: ScopedMCPServer) -> _Connection:
        fingerprint = server.connection_fingerprint()
        lock = self._open_locks.setdefault(server.pool_key, asyncio.Lock())
        async with lock:
            current = self._connections.get(server.pool_key)
            if current is not None:
                if current.alive and current.fingerprint == fingerprint:
                    return current
                reason = "config_changed" if current.alive else "connection_lost"
                await self._discard(current, reason=reason)
            connection = _Connection(server, fingerprint)
            started = time.monotonic()
            await connection.open()
            self._connections[server.pool_key] = connection
            log.info(
                "mcp_connection_opened",
                extra={
                    "msg": "MCP 服务长连接已建立",
                    "data": {
                        "scope": server.scope.key,
                        "server_name": server.name,
                        "transport": server.config.transport,
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

        if self._connections.get(connection.server.pool_key) is connection:
            del self._connections[connection.server.pool_key]
        if connection.closing:
            return
        log.info(
            "mcp_connection_discarded",
            extra={
                "msg": "MCP 服务长连接已丢弃",
                "data": {
                    "scope": connection.server.scope.key,
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
    """一个服务的长连接：owner task 持有会话上下文，直到被要求关闭或传输失败。

    会话只在本类里使用，所有请求都经 :meth:`request`：调用方在别的 task 里，只有这里能把 owner 结束的事实
    交给它们。
    """

    def __init__(self, server: ScopedMCPServer, fingerprint: str) -> None:
        self.server = server
        self.fingerprint = fingerprint
        self.in_flight = 0
        self.last_used = time.monotonic()
        self.closing = False
        self._session: ClientSession | None = None
        self._tools: list[types.Tool] | None = None
        # 握手结果：服务说明只在这里给一次，回写本机记录时与工具目录一起写。
        self._initialized: types.InitializeResult | None = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._close_requested = asyncio.Event()
        self._owner: asyncio.Task[None] | None = None
        # owner 结束的信号与原因（传输失败时的异常；被要求关闭时为 ``None``）：在途请求据此失败。
        self._ended = asyncio.Event()
        self._failure: BaseException | None = None

    @property
    def alive(self) -> bool:
        return self._session is not None and self._owner is not None and not self._owner.done()

    async def request(self, operation: Callable[[ClientSession], Awaitable[T]]) -> T:
        """在会话上发一次请求，与连接存续一起等：owner 先结束就取消请求，抛出它结束的原因。

        原因是传输失败的异常（常是包着 ``MCPAuthorizationRequired`` 或 ``HTTPStatusError`` 的异常组，由
        ``MCPFailure`` 按叶子归类）；被要求关闭时抛 ``ConnectionError``。调用方被取消时请求一并取消。

        异常:
            ConnectionError: 连接已关闭，或在请求期间被关闭。
        """

        if self._session is None or self._ended.is_set():
            raise self._end_reason()
        call = asyncio.ensure_future(operation(self._session))
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            await asyncio.wait({call, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ended.cancel()
            if not call.done():
                call.cancel()
        if call.done() and not call.cancelled():
            return call.result()
        raise self._end_reason()

    def _end_reason(self) -> BaseException:
        if self._failure is not None:
            return self._failure
        return ConnectionError(f"MCP connection to {self.server.name} is closed")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await self.request(lambda session: session.call_tool(tool_name, arguments))

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
            self._tools = (await self.request(lambda session: session.list_tools())).tools
            # 读写本机记录是文件 IO，离开池的事件循环做，不拖住其他连接。
            assert self._initialized is not None
            await asyncio.to_thread(
                MCPMachineState.record_catalog,
                self.server,
                server_catalog(self._initialized, self._tools),
            )
        return self._tools

    async def find_tool(self, tool_name: str) -> types.Tool | None:
        found = next((tool for tool in await self.tools() if tool.name == tool_name), None)
        if found is not None:
            return found
        # 服务可能新增了工具却没发 list_changed 通知：刷新一次再判定。
        self._tools = None
        return next((tool for tool in await self.tools() if tool.name == tool_name), None)

    async def _own(self) -> None:
        errlog = self._open_stderr_log(self.server) if self.server.config.transport == "stdio" else None
        try:
            async with open_client_session(
                self.server,
                errlog=errlog,
                message_handler=self._on_message,
            ) as (session, initialized):
                self._session, self._initialized = session, initialized
                self._ready.set_result(None)
                await self._close_requested.wait()
        except Exception as exc:
            self._failure = exc
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
            self._ended.set()
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

    @staticmethod
    def _open_stderr_log(server: ScopedMCPServer) -> TextIO | None:
        """打开该 stdio 服务的 stderr 日志，超限先轮转一次。

        一条连接一个文件：不在工作区里用的系统级服务写 ``<LOG_DIR>/mcp/<server>.stderr.log``；在工作区里用的
        （无论定义在哪）文件名再带上「定义作用域 + 使用的工作区」的短哈希，同名服务的不同连接不写进同一个
        文件。日志是诊断旁路：打不开时记录并返回 ``None``（退回后端 stderr），不阻断建连。
        """

        directory: Path = system_cosir.LOG_DIR / "mcp"
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", server.name)
        if server.used_in is not None:
            scope_key, used_key, _ = server.pool_key
            connection_hash = hashlib.sha256(f"{scope_key}|{used_key}".encode()).hexdigest()[:8]
            safe_name = f"{safe_name}.{connection_hash}"
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
