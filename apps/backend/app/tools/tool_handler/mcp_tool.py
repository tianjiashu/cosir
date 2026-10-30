"""Agent 侧 MCP 工具目录与调用入口。

以 thread 模式运行：连接由进程级 ``MCPConnectionPool`` 常驻持有，handler 在工具线程里把协程投到
池的事件循环上，自己轮询取消信号与调用时限（thread 工具的执行器只在执行前检查一次取消，也不做
硬超时）。取消或超时只撤销这次请求，连接保留供下次复用。
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import Any, TypeVar

from mcp import types

from app.config.logging.logger import log
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_MCP,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_MCP
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.service import depends as service_depends
from app.service.configuration.mcp_configuration import (
    MCPConfigurationError,
    MCPServerConfig,
    enabled_servers,
    read_mcp_configuration,
)
from app.tools.mcp.client import (
    MCPToolCallError,
    MCPToolNotFoundError,
    call_result_text,
    describe_exception,
    tool_catalog,
)
from app.tools.tool_models.mcp_args import MCPArgs

T = TypeVar("T")

# 单次调用（含必要的建连）的时限；thread 工具没有执行器硬超时，由 handler 自己执行。
CALL_TIMEOUT_SECONDS = 90.0
# ``list`` 并发发现各服务，单个服务的时限；慢服务只让自己的条目失败，不拖累整份目录。
DISCOVERY_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.1


class _CallStopped(Exception):
    """等待池内请求期间检出取消；请求已撤销。"""


class MCPTool(HandlerBase):
    """以一个固定工具入口提供 MCP 服务发现和工具调用。"""

    name = TOOL_MCP
    description = (
        "Discover and call tools from configured MCP servers. Use action='list' first to get "
        "server names, tool names, descriptions, and input schemas; then use action='call' "
        "with the selected server, tool, and a JSON-encoded object string in arguments. "
        "For discovery, set tool_name=null and arguments='{}'. Server connections persist "
        "across calls, so stateful servers keep their session state."
    )
    args_model = MCPArgs
    timeout_seconds = CALL_TIMEOUT_SECONDS
    group = TOOL_GROUP_MCP

    def execute(
        self,
        action: str,
        server_name: str | None = None,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """列出本机已启用且测试通过服务的工具，或调用其中一项工具。

        arguments 已由 MCPArgs 在参数入口从 JSON 字符串解析为对象；handler 不再解析，
        模型上下文保留原始字符串，执行参数对象只在当前工具调用内流转。
        """

        try:
            servers = enabled_servers(read_mcp_configuration())
        except MCPConfigurationError as exc:
            return tool_error(
                self.name,
                f"{exc}. Fix the local MCP config file, then retry.",
            )

        if action == "list":
            return self._list(servers, server_name, execution_context)

        if server_name is None or tool_name is None:
            return tool_error(
                self.name,
                "MCP call requires server_name and tool_name. "
                "Use action='list' to see servers and tools, then supply both names.",
            )
        server = next((item for item in servers if item.name == server_name), None)
        if server is None:
            return _server_not_found(server_name)
        return self._call(server, tool_name, arguments or {}, execution_context)

    def _list(
        self,
        servers: list[MCPServerConfig],
        server_name: str | None,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        selected = [
            server for server in servers if server_name is None or server.name == server_name
        ]
        if server_name is not None and not selected:
            return _server_not_found(server_name)
        if not selected:
            # 没有可用服务时不必拉起连接池的事件循环线程。
            return _catalog_observation([])
        pool = service_depends.get_mcp_connection_pool()

        async def discover(server: MCPServerConfig) -> list[types.Tool]:
            async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                return await pool.list_tools(server)

        async def discover_all() -> list[list[types.Tool] | BaseException]:
            return await asyncio.gather(
                *(discover(server) for server in selected), return_exceptions=True
            )

        try:
            outcomes = _wait(pool.submit(discover_all()), execution_context)
        except _CallStopped:
            return tool_cancelled(self.name)
        except TimeoutError:
            return tool_error(
                self.name,
                f"MCP discovery timed out after {CALL_TIMEOUT_SECONDS:.0f} seconds. "
                "Retry with server_name to discover one server.",
            )

        catalog: list[dict[str, Any]] = []
        for server, outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                error = describe_exception(outcome)
                log.error(
                    "mcp_server_discovery_failed",
                    extra={
                        "msg": "MCP 服务工具发现失败",
                        "data": {"server_name": server.name, "error": error},
                    },
                )
                catalog.append({"server_name": server.name, "error": error, "tools": []})
            else:
                catalog.append({"server_name": server.name, "tools": tool_catalog(outcome)})
        return _catalog_observation(catalog)

    def _call(
        self,
        server: MCPServerConfig,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        pool = service_depends.get_mcp_connection_pool()
        try:
            result = _wait(
                pool.submit(pool.call_tool(server, tool_name, arguments)), execution_context
            )
            content = call_result_text(result)
        except _CallStopped:
            return tool_cancelled(self.name)
        except TimeoutError:
            return tool_error(
                self.name,
                f"MCP call timed out after {CALL_TIMEOUT_SECONDS:.0f} seconds; the request was "
                "abandoned but the server may still have acted on it. Check its state before "
                "retrying.",
            )
        except MCPToolNotFoundError as exc:
            return tool_error(
                self.name,
                f"{exc}. " "List the server's tools again and use a valid tool name.",
            )
        except MCPToolCallError as exc:
            return tool_error(
                self.name,
                f"{str(exc) or 'MCP server reported a tool call failure'}. "
                "Check the arguments and the server's response before deciding whether to retry.",
            )
        except Exception as exc:
            error = describe_exception(exc)
            log.error(
                "mcp_tool_call_failed",
                extra={
                    "msg": "MCP 工具调用失败",
                    "data": {"server_name": server.name, "tool_name": tool_name, "error": error},
                },
            )
            return tool_error(
                self.name,
                f"MCP call failed: {error}. "
                "Check server status, tool name, and arguments, then retry.",
            )

        return tool_success(
            self.name,
            content,
            display_data={
                "kind": "mcp-tool-result",
                "action": "call",
                "server_name": server.name,
                "tool_name": tool_name,
            },
        )

    def to_definition(self) -> ToolDefinition:
        """返回可注册的 MCP 工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("mcp",),
            display=ToolDisplayHints(
                verb="调用 MCP",
                icon="network",
                expandable=True,
                expand_layout="details",
                default_open=False,
            ),
        )


def _wait(future: Future[T], execution_context: ToolExecutionContext | None) -> T:
    """在工具线程里等待池内请求，期间轮询取消与时限；命中任一即撤销请求。

    撤销会取消池循环上的协程（SDK 随之放弃这次请求），连接本身保留。

    异常:
        _CallStopped: 检出 Task 停止或本次调用被单独取消。
        TimeoutError: 超过 ``CALL_TIMEOUT_SECONDS``。
        其余: 池内协程抛出的异常原样上浮。
    """

    deadline = time.monotonic() + CALL_TIMEOUT_SECONDS
    while True:
        done, _ = wait([future], timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
        if done:
            return future.result()
        if execution_context is not None and should_stop_call(
            execution_context.task_id, execution_context.tool_call_id
        ):
            future.cancel()
            raise _CallStopped
        if time.monotonic() >= deadline:
            future.cancel()
            raise TimeoutError


def _catalog_observation(catalog: list[dict[str, Any]]) -> ToolObservation:
    return tool_success(
        TOOL_MCP,
        json.dumps(catalog, ensure_ascii=False, indent=2),
        display_data={"kind": "mcp-tool-result", "action": "list", "servers": catalog},
    )


def _server_not_found(server_name: str) -> ToolObservation:
    return tool_error(
        TOOL_MCP,
        f"Enabled MCP server not found: {server_name}. "
        "Use action='list' to see available servers.",
    )


def build_mcp_definition() -> ToolDefinition:
    """构造 MCP 的固定工具定义。"""

    return MCPTool().to_definition()
