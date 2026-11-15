"""MCP 服务配置响应。"""

from typing import Literal

from pydantic import BaseModel

from app.service.configuration.mcp.configuration import MCPServerConfig
from app.service.configuration.mcp.configuration_view import (
    MCPServerEntry,
    ServerStatus,
    VariableSource,
)


class MCPVariableStatusResponse(BaseModel):
    """服务引用的一个变量的值从哪来（``unset`` / ``cosir`` / ``shell``）；不返回值。"""

    name: str
    source: VariableSource
    has_default: bool


class MCPServerConfigResponse(MCPServerConfig):
    """返回给配置界面的一个可见 MCP 服务：连接定义加上本机状态。

    ``status`` 是由本机事实推出的主状态（规则见 ``configuration_view``）；``scope`` 与 ``overrides`` 的含义同
    Skill 列表项：workspace 配置页里 ``scope == "system"`` 的条目是继承来的，只读。``variables`` 与
    ``literal_fields`` 见 ``MCPServerEntry``。
    """

    status: ServerStatus
    scope: Literal["system", "workspace"]
    overrides: bool
    variables: list[MCPVariableStatusResponse]
    literal_fields: list[str]

    @classmethod
    def from_entry(cls, entry: MCPServerEntry) -> "MCPServerConfigResponse":
        server = entry.server
        return cls(
            **server.config.model_dump(),
            status=entry.status,
            scope=server.scope.kind,
            overrides=server.overrides,
            variables=[
                MCPVariableStatusResponse(
                    name=item.name, source=item.source, has_default=item.has_default
                )
                for item in entry.variables
            ],
            literal_fields=list(entry.literal_fields),
        )


class MCPServersResponse(BaseModel):
    """当前作用域可见的 MCP 服务列表。"""

    servers: list[MCPServerConfigResponse]

    @classmethod
    def from_entries(cls, entries: list[MCPServerEntry]) -> "MCPServersResponse":
        return cls(servers=[MCPServerConfigResponse.from_entry(entry) for entry in entries])
