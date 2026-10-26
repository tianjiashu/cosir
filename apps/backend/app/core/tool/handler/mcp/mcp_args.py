"""MCP 工具的统一发现与调用参数。"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Json, JsonValue, WithJsonSchema, field_validator


class MCPArgs(BaseModel):
    """通过固定的 MCP 工具入口发现服务工具或调用其中一项。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "call"] = Field(
        description="Use 'list' to discover servers and tools, or 'call' to invoke a tool."
    )
    server_name: str | None = Field(
        default=None,
        description="Server name from 'list'; use null to list all servers, required for 'call'.",
    )
    tool_name: str | None = Field(
        default=None,
        description="Tool name from the selected server; use null for 'list', required for 'call'.",
    )
    # MCP 的远端参数是开放对象；模型只提交 JSON 字符串，由同一 Pydantic 输入边界解析。
    # 对外声明 string，避免把远端任意键错误包装为 strict 的封闭对象。
    arguments: Annotated[Json[dict[str, JsonValue]], WithJsonSchema({"type": "string"})] = Field(
        description=(
            "JSON-encoded object containing arguments for the selected MCP tool. "
            "Use the string '{}' for 'list' or a call without arguments."
        ),
    )

    @field_validator("arguments")
    @classmethod
    def validate_json_numbers(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Pydantic JSON 解析允许非有限数；在同一输入边界拒绝 MCP 无法发送的值。"""
        json.dumps(value, allow_nan=False)
        return value
