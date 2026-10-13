"""Agent 侧 MCP 工具目录与调用入口。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config.logging.logger import log
from app.core.tools.schemas import (
    TOOL_MCP,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_MCP
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import MCPArgs
from app.service.mcp.mcp_client import (
    MCPConfigurationError,
    MCPToolCallError,
    MCPToolNotFoundError,
    call_server_tool,
    enabled_servers,
    list_server_tools,
    read_mcp_configuration,
)


class MCPTool(HandlerBase):
    """以一个固定工具入口提供 MCP 服务发现和工具调用。"""

    name = TOOL_MCP
    description = (
        "Discover and call tools from configured MCP servers. Use action='list' first to get "
        "server names, tool names, descriptions, and input schemas; then use action='call' "
        "with the selected server, tool, and arguments."
    )
    args_model = MCPArgs
    timeout_seconds = 90.0
    group = TOOL_GROUP_MCP

    def execute(
        self,
        action: str,
        server_name: str | None = None,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """列出本机已启用且测试通过服务的工具，或调用其中一项工具。"""

        try:
            servers = enabled_servers(read_mcp_configuration())
        except MCPConfigurationError as exc:
            return tool_error(
                self.name,
                str(exc),
                reason="修正本机 MCP 配置文件后重新调用。",
                retryable=False,
            )

        if action == "list":
            selected = [
                server for server in servers if server_name is None or server.name == server_name
            ]
            if server_name is not None and not selected:
                return tool_error(
                    self.name,
                    f"没有找到已启用的 MCP 服务：{server_name}",
                    reason="先用 action='list' 查看当前可用服务。",
                    retryable=False,
                )
            catalog: list[dict[str, Any]] = []
            for server in selected:
                try:
                    catalog.append(
                        {
                            "server_name": server.name,
                            "tools": asyncio.run(list_server_tools(server)),
                        }
                    )
                except Exception as exc:
                    log.error(
                        "mcp_server_discovery_failed",
                        extra={
                            "msg": "MCP 服务工具发现失败",
                            "data": {"server_name": server.name, "error_type": type(exc).__name__},
                        },
                    )
                    catalog.append({"server_name": server.name, "error": "连接失败", "tools": []})
            return tool_success(
                self.name,
                json.dumps(catalog, ensure_ascii=False, indent=2),
                display_data={"kind": "mcp-tool-result", "action": "list", "servers": catalog},
            )

        if server_name is None or tool_name is None:
            return tool_error(
                self.name,
                "MCP 调用需要 server_name 和 tool_name。",
                reason="先用 action='list' 查看服务与工具，再提供这两个名称。",
                retryable=False,
            )
        server = next((item for item in servers if item.name == server_name), None)
        if server is None:
            return tool_error(
                self.name,
                f"没有找到已启用的 MCP 服务：{server_name}",
                reason="先用 action='list' 查看当前可用服务。",
                retryable=False,
            )

        try:
            content = asyncio.run(call_server_tool(server, tool_name, arguments or {}))
        except MCPToolNotFoundError as exc:
            return tool_error(
                self.name,
                str(exc),
                reason="重新列出该服务的工具，并使用有效工具名。",
                retryable=True,
            )
        except MCPToolCallError as exc:
            return tool_error(
                self.name,
                str(exc) or "MCP 服务报告工具调用失败。",
                reason="检查参数和 MCP 服务返回后决定是否重试。",
                retryable=True,
            )
        except Exception as exc:
            log.error(
                "mcp_tool_call_failed",
                extra={
                    "msg": "MCP 工具调用失败",
                    "data": {
                        "server_name": server.name,
                        "tool_name": tool_name,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                self.name,
                f"MCP 调用失败：{type(exc).__name__}",
                reason="检查服务状态、工具名和参数后重试。",
                retryable=True,
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
            execution_mode="process",
            display=ToolDisplayHints(
                verb="调用 MCP",
                icon="network",
                expandable=True,
                expand_layout="details",
                default_open=False,
            ),
        )


def build_mcp_definition() -> ToolDefinition:
    """构造 MCP 的固定工具定义。"""

    return MCPTool().to_definition()
