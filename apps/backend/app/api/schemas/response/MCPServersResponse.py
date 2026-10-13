"""系统级 MCP 服务配置响应。"""

from typing import Literal

from pydantic import BaseModel


class MCPServerConfigResponse(BaseModel):
    """返回给配置界面的 MCP 服务配置。"""

    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    command: str
    args: list[str]
    env: dict[str, str]
    url: str
    headers: dict[str, str]
    enabled: bool
    tested: bool


class MCPServersResponse(BaseModel):
    """当前已保存的 MCP 服务配置列表。"""

    servers: list[MCPServerConfigResponse]
