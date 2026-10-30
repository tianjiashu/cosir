"""按服务配置建立 MCP 客户端会话，并把 SDK 结果归一化为模型可读文本。

长连接与短连接共用这里的会话建立：Agent 调用经 ``connection_pool`` 持有长连接；配置界面的连接
测试测的是尚未保存的配置，走 :func:`list_server_tools` 的一次性短连接，不进连接池。
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TextIO

import anyio
import httpx
from mcp import ClientSession, McpError, types
from mcp.client.session import MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from app.service.configuration.mcp_configuration import MCPServerConfig

# 异常说明进入模型上下文与日志，只保留足够定位的长度。
_MAX_ERROR_CHARS = 300


class MCPToolNotFoundError(LookupError):
    """服务中不存在请求的 MCP 工具。"""

    def __init__(self, server_name: str, tool_name: str) -> None:
        super().__init__(f"MCP server {server_name} does not expose tool {tool_name}")


class MCPToolCallError(RuntimeError):
    """MCP 服务将工具调用作为错误结果（``isError``）返回；连接本身完好。"""


@asynccontextmanager
async def open_client_session(
    server: MCPServerConfig,
    *,
    errlog: TextIO | None = None,
    message_handler: MessageHandlerFnT | None = None,
) -> AsyncIterator[ClientSession]:
    """按服务配置建立并初始化 MCP 会话，退出时释放传输资源（stdio 会结束服务子进程）。

    SDK 的会话与传输基于 anyio task group：进入与退出必须发生在同一个 asyncio task 内，
    长连接因此由连接池里每个服务一个的 owner task 持有本上下文。

    参数:
        errlog: stdio 服务子进程的 stderr 去向，需要真实文件描述符；缺省为后端 stderr；
            HTTP 传输忽略。
        message_handler: 服务端主动消息（如 ``tools/list_changed`` 通知）与读流异常的回调。
    """

    if server.transport == "stdio":
        async with (
            stdio_client(_stdio_parameters(server), errlog=errlog or sys.stderr) as (read, write),
            ClientSession(read, write, message_handler=message_handler) as client,
        ):
            await client.initialize()
            yield client
        return

    if server.transport == "sse":
        async with (
            sse_client(server.url, headers=server.headers) as (read, write),
            ClientSession(read, write, message_handler=message_handler) as client,
        ):
            await client.initialize()
            yield client
        return

    http_client = create_mcp_http_client(headers=server.headers)
    async with (
        http_client,
        streamable_http_client(server.url, http_client=http_client) as (read, write, _),
        ClientSession(read, write, message_handler=message_handler) as client,
    ):
        await client.initialize()
        yield client


async def list_server_tools(server: MCPServerConfig) -> list[dict[str, Any]]:
    """短连接到一个 MCP 服务并返回工具目录；只供配置界面的连接测试使用。"""

    async with open_client_session(server) as client:
        result = await client.list_tools()
    return tool_catalog(result.tools)


def tool_catalog(tools: list[types.Tool]) -> list[dict[str, Any]]:
    """把 SDK 工具列表投影为模型与配置界面共用的目录条目。"""

    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        }
        for tool in tools
    ]


def call_result_text(result: types.CallToolResult) -> str:
    """归一化工具调用结果；``isError`` 结果抛 :class:`MCPToolCallError`。

    结构化结果优先（JSON 文本），否则拼接文本块；非文本块只列类型，避免回传二进制载荷。
    """

    if result.isError:
        raise MCPToolCallError(_content_text(result.content))
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False, indent=2)
    return _content_text(result.content)


def is_connection_failure(exc: BaseException) -> bool:
    """判断异常是否意味着会话已不可用（需要丢弃连接、下次重连），而非单次请求失败。

    服务端返回的 JSON-RPC 错误与 ``isError`` 结果都不算：连接完好，复用即可。
    """

    leaf = _leaf_exception(exc)
    if isinstance(leaf, McpError):
        return leaf.error.code == types.CONNECTION_CLOSED
    return isinstance(
        leaf,
        anyio.ClosedResourceError
        | anyio.BrokenResourceError
        | anyio.EndOfStream
        | httpx.TransportError
        | OSError,
    )


def describe_exception(exc: BaseException) -> str:
    """给模型与日志的简短异常说明；展开 anyio 的 ``ExceptionGroup``，只取首个叶子异常。

    task group 抛出的异常组本身的类型名不含任何定位信息，真正的原因在叶子异常里。
    """

    leaf = _leaf_exception(exc)
    message = str(leaf).strip()
    text = f"{type(leaf).__name__}: {message}" if message else type(leaf).__name__
    return text[:_MAX_ERROR_CHARS]


def _leaf_exception(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _stdio_parameters(server: MCPServerConfig) -> StdioServerParameters:
    """只把服务显式配置的变量传给子进程；SDK 会补入命令启动所需的默认环境白名单。"""

    return StdioServerParameters(
        command=server.command,
        args=server.args,
        env=server.env,
    )


def _content_text(content: list[types.ContentBlock]) -> str:
    """保留 MCP 文本块供模型使用，其他块只列出类型避免回传二进制载荷。"""

    parts: list[str] = []
    for item in content:
        if isinstance(item, types.TextContent):
            parts.append(item.text)
        else:
            parts.append(f"[{item.type} content returned]")
    return "\n".join(parts)
