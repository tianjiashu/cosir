"""MCP 工具的统一发现与调用参数。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MCPArgs(BaseModel):
    """通过固定的 MCP 工具入口发现服务工具或调用其中一项。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "call"] = Field(
        description="Use 'list' to discover servers and tools, or 'call' to invoke a tool."
    )
    server_name: str | None = Field(
        default=None,
        description="Server name from 'list'; optional for 'list', required for 'call'.",
    )
    tool_name: str | None = Field(
        default=None,
        description="Tool name from the selected server; required for 'call'.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON arguments to pass to the selected tool.",
    )
