"""MCP 服务配置响应。"""

from typing import Literal

from pydantic import BaseModel


class MCPServerConfigResponse(BaseModel):
    """返回给配置界面的一个可见 MCP 服务。

    ``tested`` 取自本机测试记录（当前连接配置是否测试通过）；``scope`` 与 ``overrides`` 的含义同
    Skill 列表项：workspace 配置页里 ``scope == "system"`` 的条目是继承来的，只读。
    """

    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    command: str
    args: list[str]
    env: dict[str, str]
    url: str
    headers: dict[str, str]
    enabled: bool
    tested: bool
    scope: Literal["system", "workspace"]
    overrides: bool


class MCPServersResponse(BaseModel):
    """当前作用域可见的 MCP 服务列表。"""

    servers: list[MCPServerConfigResponse]
