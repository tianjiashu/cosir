"""MCP 服务配置的数据契约、读取规则与 Agent 可用视图。

只描述「连哪些服务、怎么连、哪些对 Agent 可用」；建立连接与会话由 ``app/tools/mcp/`` 负责。
放在 service 层是因为 ``core/context`` 构建系统提示词目录也要读它，而 core 不得导入 ``app.tools``。

两个作用域（系统级、workspace 级）各有一份 ``mcp_servers.json``，只放连接定义；「本机测试通过」
不在这里，而在系统级的测试记录（``mcp_tested_servers``）里，workspace 文件随仓库共享也带不进来。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config.logging.logger import log
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp_tested_servers import MCPTestedServers


class MCPServerConfig(BaseModel):
    """一个 MCP 服务的连接配置。"""

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
    """一个作用域 ``mcp_servers.json`` 的顶层结构：只有服务列表。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    servers: list[MCPServerConfig] = Field(default_factory=list)


class MCPConfigurationError(ValueError):
    """MCP 配置文件无法解析或违反配置契约。"""


@dataclass(frozen=True)
class ScopedMCPServer:
    """一个落在具体作用域里的 MCP 服务：连接配置加上它从哪来、在哪运行。

    ``overrides`` 表示 workspace 层的服务遮住了系统层的同名服务。连接池按 :attr:`pool_key`
    持有连接，因此两个 workspace 的同名服务各用各的连接。
    """

    scope: ExtensionScope
    config: MCPServerConfig
    overrides: bool = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def cwd(self) -> Path | None:
        """stdio 子进程的工作目录：workspace 服务在工作区根目录运行，系统级服务沿用后端进程目录。"""

        return self.scope.workspace_root

    @property
    def pool_key(self) -> tuple[str, str]:
        return (self.scope.key, self.config.name)

    @property
    def fingerprint(self) -> str:
        return server_connection_fingerprint(self.config)


def read_mcp_configuration(scope: ExtensionScope) -> MCPConfiguration:
    """读取一个作用域的 MCP 配置；尚未创建配置文件时视为空服务目录。"""

    path = scope.mcp_servers_file
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MCPConfiguration()
    except OSError as exc:
        log.error(
            "mcp_configuration_read_failed",
            extra={
                "msg": "读取 MCP 配置失败",
                "data": {"scope": scope.key, "error_type": type(exc).__name__},
            },
        )
        raise MCPConfigurationError("MCP 配置文件无法读取") from exc

    try:
        configuration = MCPConfiguration.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        log.error(
            "mcp_configuration_invalid",
            extra={
                "msg": "MCP 配置文件格式无效",
                "data": {"scope": scope.key, "error_type": type(exc).__name__},
            },
        )
        raise MCPConfigurationError("MCP 配置文件格式无效") from exc

    names = [server.name for server in configuration.servers]
    if len(names) != len(set(names)):
        log.error(
            "mcp_configuration_invalid",
            extra={
                "msg": "MCP 配置文件中的服务名称重复",
                "data": {"scope": scope.key, "server_count": len(names)},
            },
        )
        raise MCPConfigurationError("MCP 配置中的服务名称必须唯一")
    return configuration


def visible_servers(workspace_root: str | Path | None) -> list[ScopedMCPServer]:
    """某个工作区（``None`` 为系统级）看得到的服务：workspace 层同名覆盖系统层。

    顺序：未被覆盖的系统服务按配置顺序在前，workspace 服务按配置顺序在后。
    """

    system = ExtensionScope.system()
    system_servers = [ScopedMCPServer(system, item) for item in read_mcp_configuration(system).servers]
    if workspace_root is None:
        return system_servers
    workspace = ExtensionScope.workspace(workspace_root)
    workspace_configs = read_mcp_configuration(workspace).servers
    workspace_names = {item.name for item in workspace_configs}
    system_names = {item.name for item in system_servers}
    return [server for server in system_servers if server.name not in workspace_names] + [
        ScopedMCPServer(workspace, item, overrides=item.name in system_names)
        for item in workspace_configs
    ]


def available_servers(workspace_root: str | Path | None) -> list[ScopedMCPServer]:
    """对 Agent 可用的服务：可见、启用，并且当前连接配置在本机测试通过。"""

    tested = MCPTestedServers.read()
    return [
        server
        for server in visible_servers(workspace_root)
        if server.config.enabled and tested.passed(server)
    ]


def server_connection_fingerprint(server: MCPServerConfig) -> str:
    """按连接参数生成稳定标识；启停开关变化不需要重新测试连接。"""

    connection = server.model_dump(mode="json", exclude={"enabled"})
    serialized = json.dumps(connection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
