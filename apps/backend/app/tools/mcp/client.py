"""按服务配置建立 MCP 客户端会话，并给出工具目录与连接失败的判定。

长连接与短连接共用这里的会话建立：Agent 调用经 ``connection_pool`` 持有长连接；配置界面的连接
测试测的是尚未保存的配置，走 :func:`list_server_catalog` 的一次性短连接，不进连接池。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import anyio
import httpx
from mcp import ClientSession, McpError, types
from mcp.client.session import ListRootsFnT, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import FileUrl

from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPServerCatalog, MCPToolEntry
from app.tools.mcp.oauth.provider import (
    AuthorizationInteraction,
    oauth_auth,
)

# 异常说明进入模型上下文与日志，只保留足够定位的长度。
_MAX_ERROR_CHARS = 300


class MCPToolArgumentsError(ValueError):
    """调用参数不符合服务声明的输入 schema；请求没有发给服务。``schema`` 随报错交给模型照着改。"""

    def __init__(self, server_name: str, tool_name: str, reason: str, schema: dict[str, Any]) -> None:
        self.schema = schema
        super().__init__(f"Arguments for {server_name}/{tool_name} were rejected: {reason}")


class MCPToolNotFoundError(LookupError):
    """服务中不存在请求的 MCP 工具。"""

    def __init__(self, server_name: str, tool_name: str) -> None:
        super().__init__(f"MCP server {server_name} does not expose tool {tool_name}")


@asynccontextmanager
async def open_client_session(
    server: ScopedMCPServer,
    *,
    errlog: TextIO | None = None,
    message_handler: MessageHandlerFnT | None = None,
    interaction: AuthorizationInteraction | None = None,
) -> AsyncIterator[tuple[ClientSession, types.InitializeResult]]:
    """按作用域内的服务配置建立并初始化 MCP 会话，退出时释放传输资源（stdio 会结束服务子进程）。

    同时交出 initialize 的结果：服务说明（``instructions``）只在握手时给一次，SDK 会话不保存它。

    SDK 的会话与传输基于 anyio task group：进入与退出必须发生在同一个 asyncio task 内，
    长连接因此由连接池里每个服务一个的 owner task 持有本上下文。

    参数:
        errlog: stdio 服务子进程的 stderr 去向，需要真实文件描述符；缺省为后端 stderr；
            HTTP 传输忽略。
        message_handler: 服务端主动消息（如 ``tools/list_changed`` 通知）与读流异常的回调。
        interaction: 只有配置页的连接测试传入：服务需要授权时经它打开浏览器、等回调；缺省（连接池）
            不交互，需要授权即失败（见 ``oauth/provider``）。

    异常:
        MissingVariablesError: 配置引用的环境变量未设置（建连前就失败，不拉起子进程）。
        MCPAuthorizationRequired: 不交互时服务需要用户授权（可能包在 ``ExceptionGroup`` 里，归类见
            ``failure.MCPFailure``）。
    """

    config = server.connection_config()
    roots = _roots_callback(server.used_in) if server.used_in is not None else None
    if config.transport == "stdio":
        async with (
            stdio_client(_stdio_parameters(config, server.cwd), errlog=errlog or sys.stderr) as (
                read,
                write,
            ),
            ClientSession(
                read, write, message_handler=message_handler, list_roots_callback=roots
            ) as client,
        ):
            yield client, await client.initialize()
        return

    auth = oauth_auth(server, config, interaction)
    if config.transport == "sse":
        async with (
            sse_client(config.url, headers=config.headers, auth=auth) as (read, write),
            ClientSession(
                read, write, message_handler=message_handler, list_roots_callback=roots
            ) as client,
        ):
            yield client, await client.initialize()
        return

    http_client = create_mcp_http_client(headers=config.headers, auth=auth)
    async with (
        http_client,
        streamable_http_client(config.url, http_client=http_client) as (read, write, _),
        ClientSession(
            read, write, message_handler=message_handler, list_roots_callback=roots
        ) as client,
    ):
        yield client, await client.initialize()


async def list_server_catalog(
    server: ScopedMCPServer, interaction: AuthorizationInteraction | None = None
) -> MCPServerCatalog:
    """短连接到一个 MCP 服务并返回它的说明与工具目录；只供配置界面的连接测试使用（可交互授权）。"""

    async with open_client_session(server, interaction=interaction) as (client, initialized):
        result = await client.list_tools()
    return server_catalog(initialized, result.tools)


def server_catalog(initialized: types.InitializeResult, tools: list[types.Tool]) -> MCPServerCatalog:
    """SDK 的握手结果与工具描述 → 本机记录里的服务目录（测试与连接池回写共用）。"""

    return MCPServerCatalog(
        instructions=initialized.instructions or "", tools=[tool_entry(tool) for tool in tools]
    )


def tool_entry(tool: types.Tool) -> MCPToolEntry:
    """SDK 的工具描述 → 本机记录里的目录条目（配置页测试结果与 Agent 的延迟工具目录都读它）。"""

    return MCPToolEntry(
        name=tool.name, description=tool.description or "", input_schema=dict(tool.inputSchema)
    )


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


def _roots_callback(workspace_root: Path) -> ListRootsFnT:
    """告诉服务它在哪个工作区里用（MCP roots）：filesystem、git 这类服务据此确定操作范围。

    一条连接只属于一个工作区（见 ``ScopedMCPServer.used_in``），roots 在连接存续期间不变，所以不发
    ``roots/list_changed``。不在工作区里用（系统级配置页的测试）时不传回调，SDK 不声明 roots 能力。
    """

    root = types.Root(uri=FileUrl(workspace_root.resolve().as_uri()), name=workspace_root.name)

    async def list_roots(_context: object) -> types.ListRootsResult:
        return types.ListRootsResult(roots=[root])

    return list_roots


def _stdio_parameters(config: MCPServerConfig, cwd: Path | None) -> StdioServerParameters:
    """只把服务显式配置的变量传给子进程；SDK 会补入命令启动所需的默认环境白名单。

    ``config`` 是变量已展开的建连配置：要交给服务的密钥写成 ``env: {"TOKEN": "${TOKEN}"}``，不整包
    继承后端环境（那里有宿主契约变量与 cosir 自己的配置）。workspace 服务以工作区根目录为 cwd
    （``npx`` 类服务常按 cwd 找项目文件）；系统级服务不属于任何工作区，沿用后端进程目录。
    """

    return StdioServerParameters(
        command=config.command,
        args=config.args,
        env=config.env,
        cwd=cwd,
    )
