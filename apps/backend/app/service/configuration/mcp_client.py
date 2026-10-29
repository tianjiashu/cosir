"""面向本机单用户 Agent 的 MCP 客户端与传输配置。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config.logging.logger import log
from app.utils.path.system_cosir import system_mcp_servers_file


class MCPServerConfig(BaseModel):
    """一个 MCP 服务的本机连接配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_transport_configuration(self) -> MCPServerConfig:
        """要求所选传输具备对应连接入口。"""

        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP 服务必须配置 command")
        if self.transport != "stdio" and not self.url.strip():
            raise ValueError("HTTP MCP 服务必须配置 url")
        if self.transport != "stdio" and not self.url.casefold().startswith(
            ("http://", "https://")
        ):
            raise ValueError("HTTP MCP 服务 url 必须使用 http 或 https")
        return self


class MCPConfiguration(BaseModel):
    """系统级 MCP 服务配置文件的顶层结构。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    servers: list[MCPServerConfig] = Field(default_factory=list)
    tested_servers: dict[str, str] = Field(default_factory=dict)


class MCPConfigurationError(ValueError):
    """MCP 配置文件无法解析或违反配置契约。"""


def read_mcp_configuration() -> MCPConfiguration:
    """读取本机 MCP 配置；尚未创建配置文件时视为空服务目录。"""

    path = system_mcp_servers_file()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MCPConfiguration()
    except OSError as exc:
        log.error(
            "mcp_configuration_read_failed",
            extra={"msg": "读取 MCP 配置失败", "data": {"error_type": type(exc).__name__}},
        )
        raise MCPConfigurationError("MCP 配置文件无法读取") from exc

    try:
        configuration = MCPConfiguration.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        log.error(
            "mcp_configuration_invalid",
            extra={
                "msg": "MCP 配置文件格式无效",
                "data": {"error_type": type(exc).__name__},
            },
        )
        raise MCPConfigurationError("MCP 配置文件格式无效") from exc

    names = [server.name for server in configuration.servers]
    if len(names) != len(set(names)):
        log.error(
            "mcp_configuration_invalid",
            extra={
                "msg": "MCP 配置文件中的服务名称重复",
                "data": {"server_count": len(names)},
            },
        )
        raise MCPConfigurationError("MCP 配置中的服务名称必须唯一")
    return configuration


def enabled_servers(configuration: MCPConfiguration) -> list[MCPServerConfig]:
    """返回启用且连接测试成功的 MCP 服务，保持配置文件中的顺序。"""

    return [
        server
        for server in configuration.servers
        if server.enabled
        and configuration.tested_servers.get(server.name) == server_connection_fingerprint(server)
    ]


def server_connection_fingerprint(server: MCPServerConfig) -> str:
    """按连接参数生成稳定标识；启停开关变化不需要重新测试连接。"""

    connection = server.model_dump(mode="json", exclude={"enabled"})
    serialized = json.dumps(connection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def list_server_tools(server: MCPServerConfig) -> list[dict[str, Any]]:
    """短连接到一个 MCP 服务并返回模型可读的工具目录。"""

    async with _client_session(server) as client:
        result = await client.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        }
        for tool in result.tools
    ]


async def call_server_tool(
    server: MCPServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """短连接到一个 MCP 服务，校验工具存在后执行并归一化返回内容。"""

    async with _client_session(server) as client:
        tools = await client.list_tools()
        if not any(tool.name == tool_name for tool in tools.tools):
            raise MCPToolNotFoundError(server.name, tool_name)
        result = await client.call_tool(tool_name, arguments)

    if result.isError:
        raise MCPToolCallError(_content_text(result.content))
    structured_content = getattr(result, "structuredContent", None)
    if structured_content is not None:
        return json.dumps(structured_content, ensure_ascii=False, indent=2)
    return _content_text(result.content)


class MCPToolNotFoundError(LookupError):
    """服务中不存在请求的 MCP 工具。"""

    def __init__(self, server_name: str, tool_name: str) -> None:
        super().__init__(f"服务 {server_name} 未公开工具 {tool_name}")


class MCPToolCallError(RuntimeError):
    """MCP 服务将工具调用作为错误结果返回。"""


def _stdio_parameters(server: MCPServerConfig) -> StdioServerParameters:
    """只把服务显式配置的变量传给子进程；SDK 会补入命令启动所需的默认环境白名单。"""

    return StdioServerParameters(
        command=server.command,
        args=server.args,
        env=server.env,
    )


@asynccontextmanager
async def _client_session(server: MCPServerConfig) -> AsyncIterator[ClientSession]:
    """按服务配置建立 MCP 客户端会话，统一初始化和释放传输资源。"""

    if server.transport == "stdio":
        async with (
            stdio_client(_stdio_parameters(server)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            yield client
        return

    if server.transport == "sse":
        async with (
            sse_client(server.url, headers=server.headers) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            yield client
        return

    http_client = create_mcp_http_client(headers=server.headers)
    async with (
        http_client,
        streamable_http_client(server.url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


def _content_text(content: list[Any]) -> str:
    """保留 MCP 文本块供模型使用，其他块只列出类型避免回传二进制载荷。"""

    parts: list[str] = []
    for item in content:
        if item.type == "text":
            parts.append(item.text)
        else:
            parts.append(f"[{item.type} content returned]")
    return "\n".join(parts)
