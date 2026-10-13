"""MCP 服务配置请求体。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPServerConfigRequest(BaseModel):
    """HTTP 层接收的单个 MCP 服务配置。"""

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
    def validate_transport_configuration(self) -> MCPServerConfigRequest:
        """要求所选传输提供对应的连接入口。"""

        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP 服务必须配置 command")
        if self.transport != "stdio" and not self.url.strip():
            raise ValueError("HTTP MCP 服务必须配置 url")
        if self.transport != "stdio" and not self.url.casefold().startswith(
            ("http://", "https://")
        ):
            raise ValueError("HTTP MCP 服务 url 必须使用 http 或 https")
        return self
