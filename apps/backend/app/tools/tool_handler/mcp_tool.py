"""Agent 侧 MCP 工具目录与调用入口。

以 thread 模式运行：连接由进程级 ``MCPConnectionPool`` 常驻持有，handler 在工具线程里把协程投到
池的事件循环上，经 ``wait_for_call`` 等待并轮询取消信号与调用时限（thread 工具的执行器只在执行前
检查一次取消，也不做硬超时）。取消或超时只撤销这次请求，连接保留供下次复用。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import types

from app.config.logging.logger import log
from app.core.tool.execute.thread_wait import ToolCallStopped, wait_for_call
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_MCP,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_MCP
from app.service import depends as service_depends
from app.service.configuration.mcp.configuration import (
    MCPConfigurationError,
    ScopedMCPServer,
    available_servers,
)
from app.tools.display.mcp_display import (
    build_mcp_call_display_data,
    build_mcp_catalog_display_data,
)
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.client import (
    MCPToolArgumentsError,
    MCPToolNotFoundError,
    tool_detail,
    tool_summary,
)
from app.tools.mcp.failure import MCPFailure
from app.tools.mcp.result_images import StoredResultImages
from app.tools.tool_models.mcp_args import MCPArgs

# 单次调用（含必要的建连）的时限；thread 工具没有执行器硬超时，由 handler 自己执行。
CALL_TIMEOUT_SECONDS = 90.0
# ``list`` 并发发现各服务，单个服务的时限；慢服务只让自己的条目失败，不拖累整份目录。
DISCOVERY_TIMEOUT_SECONDS = 20.0


class MCPTool(HandlerBase):
    """以一个固定工具入口提供 MCP 服务发现和工具调用。"""

    name = TOOL_MCP
    # 中文译文：发现并调用已配置 MCP 服务的工具。action='list' 返回各服务的工具名与简短说明，可用
    # server_name 只看一个服务、用 query 按关键词筛；同时给 server_name 与 tool_name 时返回这个工具的完整
    # 输入 schema。action='call' 调用工具，arguments 是 JSON 对象字符串；参数不合格时本地拒绝，报错里带
    # schema。list 时 arguments 传 '{}'。连接在多次调用间保持，有状态的服务会保留会话状态。
    description = (
        "Discover and call tools from configured MCP servers. action='list' returns each server's "
        "tool names with short descriptions; narrow it with server_name and with query (keywords "
        "matched against tool names and descriptions). Set both server_name and tool_name in "
        "'list' to get that tool's full input schema. action='call' invokes a tool with arguments "
        "as a JSON-encoded object string; arguments that do not match the tool's schema are "
        "rejected before reaching the server, and the error includes the schema. Use "
        "arguments='{}' for 'list'. Server connections persist across calls, so stateful servers "
        "keep their session state."
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
        query: str | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """列出当前工作区可用服务（系统级与 workspace 级合并、启用且本机测试通过）的工具，或调用其中一项。

        arguments 已由 MCPArgs 在参数入口从 JSON 字符串解析为对象；handler 不再解析，
        模型上下文保留原始字符串，执行参数对象只在当前工具调用内流转。
        """

        try:
            servers = available_servers(execution_context.workspace_root)
        except MCPConfigurationError as exc:
            return tool_error(
                self.name,
                f"{exc}. Fix the local MCP config file, then retry.",
            )

        if action == "list":
            if tool_name is not None and server_name is None:
                return tool_error(
                    self.name,
                    "server_name is required to look up a tool's schema. "
                    "Use action='list' to find which server has the tool.",
                )
            return self._list(servers, server_name, tool_name, query, execution_context)

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
        servers: list[ScopedMCPServer],
        server_name: str | None,
        tool_name: str | None,
        query: str | None,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """目录（名称与简短说明，可按关键词筛）或一个工具的完整 schema（给了 ``tool_name`` 时）。"""
        selected = [
            server for server in servers if server_name is None or server.name == server_name
        ]
        if server_name is not None and not selected:
            return _server_not_found(server_name)
        if not selected:
            # 没有可用服务时不必拉起连接池的事件循环线程。
            return _catalog_observation([])
        pool = service_depends.get_mcp_connection_pool()

        async def discover(server: ScopedMCPServer) -> list[types.Tool]:
            async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                return await pool.list_tools(server)

        async def discover_all() -> list[list[types.Tool] | BaseException]:
            return await asyncio.gather(
                *(discover(server) for server in selected), return_exceptions=True
            )

        try:
            outcomes = wait_for_call(
                pool.submit(discover_all()), execution_context, CALL_TIMEOUT_SECONDS
            )
        except ToolCallStopped:
            return tool_cancelled(self.name)
        except TimeoutError:
            log.warning(
                "mcp_discovery_timed_out",
                extra={
                    "msg": "MCP 工具发现超时",
                    "data": {
                        "session_id": execution_context.session_id,
                        "servers": [server.pool_key for server in selected],
                        "timeout_seconds": CALL_TIMEOUT_SECONDS,
                    },
                },
            )
            return tool_error(
                self.name,
                f"MCP discovery timed out after {CALL_TIMEOUT_SECONDS:.0f} seconds. "
                "Retry with server_name to discover one server.",
            )

        catalog: list[dict[str, Any]] = []
        for server, outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                error = MCPFailure.from_exception(outcome, server).for_model()
                log.error(
                    "mcp_server_discovery_failed",
                    extra={
                        "msg": "MCP 服务工具发现失败",
                        "data": {"scope": server.scope.key, "server_name": server.name, "error": error},
                    },
                )
                catalog.append(
                    {"server_name": server.name, "scope": server.scope.kind, "error": error, "tools": []}
                )
            else:
                catalog.append(
                    {
                        "server_name": server.name,
                        "scope": server.scope.kind,
                        "tools": _catalog_tools(outcome, tool_name, query),
                    }
                )
        if tool_name is not None and catalog and "error" not in catalog[0] and not catalog[0]["tools"]:
            return tool_error(
                self.name,
                f"MCP server {server_name} has no tool named {tool_name}. "
                "Use action='list' with this server_name to see its tools.",
            )
        return _catalog_observation(catalog)

    def _call(
        self,
        server: ScopedMCPServer,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        pool = service_depends.get_mcp_connection_pool()
        try:
            result = MCPCallResult.from_sdk(
                wait_for_call(
                    pool.submit(pool.call_tool(server, tool_name, arguments)),
                    execution_context,
                    CALL_TIMEOUT_SECONDS,
                )
            )
        except ToolCallStopped:
            return tool_cancelled(self.name)
        except TimeoutError:
            _log_call_failure(server, tool_name, execution_context, "timeout")
            return tool_error(
                self.name,
                f"MCP call timed out after {CALL_TIMEOUT_SECONDS:.0f} seconds; the request was "
                "abandoned but the server may still have acted on it. Check its state before "
                "retrying.",
            )
        except MCPToolArgumentsError as exc:
            _log_call_failure(server, tool_name, execution_context, "invalid_arguments", error=str(exc))
            return tool_error(
                self.name,
                f"{exc}. The request was not sent. Fix the arguments to match this input schema "
                f"and call again: {json.dumps(exc.schema, ensure_ascii=False)}",
            )
        except MCPToolNotFoundError as exc:
            _log_call_failure(server, tool_name, execution_context, "tool_not_found")
            return tool_error(
                self.name,
                f"{exc}. " "List the server's tools again and use a valid tool name.",
            )
        except Exception as exc:
            failure = MCPFailure.from_exception(exc, server)
            _log_call_failure(server, tool_name, execution_context, failure.kind, error=failure.detail)
            return tool_error(self.name, failure.for_model())

        if result.is_error:
            _log_call_failure(server, tool_name, execution_context, "server_reported_error")
            return tool_error(
                self.name,
                f"{result.text or 'MCP server reported a tool call failure'}. "
                "Check the arguments and the server's response before deciding whether to retry.",
            )
        return _call_observation(server, tool_name, result, execution_context)

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
        )


def _call_observation(
    server: ScopedMCPServer,
    tool_name: str,
    result: MCPCallResult,
    execution_context: ToolExecutionContext,
) -> ToolObservation:
    """成功结果：图片存进工作区附件（``StoredResultImages``），经工具图片通道交给模型。

    MCP 工具只是有时带图，不能像专门看图的工具那样声明 ``returns_images`` 由门禁整个拒绝；模型
    看不了图时由 observe 统一改写成说明（``withhold_unviewable_images``），这里不判断模型能力。
    """

    images = StoredResultImages.store(
        result.images,
        workspace_id=execution_context.workspace_id,
        name=f"mcp-{server.name}",
        log_data={
            "session_id": execution_context.session_id,
            "server_name": server.name,
            "tool_name": tool_name,
        },
    )
    return tool_success(
        TOOL_MCP,
        "\n".join(part for part in [result.text, *images.notes] if part),
        display_data=build_mcp_call_display_data(
            server_name=server.name,
            tool_name=tool_name,
            scope=server.scope.kind,
            images=images.asset_ids,
        ),
        images=images.paths,
    )


def _catalog_tools(tools: list[types.Tool], tool_name: str | None, query: str | None) -> list[dict[str, Any]]:
    """给了 ``tool_name`` 只回这一个工具的完整条目；否则回摘要，``query`` 的每个词都要出现在名称或说明里。"""

    if tool_name is not None:
        return [tool_detail(tool) for tool in tools if tool.name == tool_name]
    terms = (query or "").casefold().split()
    return [
        tool_summary(tool)
        for tool in tools
        if all(term in f"{tool.name} {tool.description or ''}".casefold() for term in terms)
    ]


def _catalog_observation(catalog: list[dict[str, Any]]) -> ToolObservation:
    return tool_success(
        TOOL_MCP,
        json.dumps(catalog, ensure_ascii=False, indent=2),
        display_data=build_mcp_catalog_display_data(catalog),
    )


def _server_not_found(server_name: str) -> ToolObservation:
    # 可用 = 已配置、已启用且当前连接定义在本机测试通过（``available_servers``），三者缺一都落到这里。
    return tool_error(
        TOOL_MCP,
        f"MCP server not available: {server_name}. It is not configured, is disabled, or has not "
        "passed a connection test on this machine. Use action='list' to see available servers.",
    )


def _log_call_failure(
    server: ScopedMCPServer,
    tool_name: str,
    execution_context: ToolExecutionContext,
    reason: str,
    *,
    error: str | None = None,
) -> None:
    """调用失败的唯一日志出口：执行器不记录 handler 返回的错误观察，这里不记就查不到。"""

    log.warning(
        "mcp_tool_call_failed",
        extra={
            "msg": "MCP 工具调用失败",
            "data": {
                "session_id": execution_context.session_id,
                "tool_call_id": execution_context.tool_call_id,
                "scope": server.scope.key,
                "server_name": server.name,
                "tool_name": tool_name,
                "reason": reason,
                "error": error,
            },
        },
    )


def build_mcp_definition() -> ToolDefinition:
    """构造 MCP 的固定工具定义。"""

    return MCPTool().to_definition()
