"""系统级 MCP 服务配置的数据契约与读取规则。

只描述「连哪些服务、怎么连、哪些已通过连接测试」；建立连接与会话由 ``app/tools/mcp/`` 负责。
放在 service 层是因为 ``core/context`` 构建系统提示词目录也要读它，而 core 不得导入 ``app.tools``。
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

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
