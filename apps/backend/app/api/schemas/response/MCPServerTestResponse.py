"""MCP 服务连接测试响应。"""

from typing import Any

from pydantic import BaseModel


class MCPServerTestResponse(BaseModel):
    """MCP 连接测试结果及发现的工具。"""

    success: bool
    tools: list[dict[str, Any]]
    error: str | None = None
    agent_available: bool = False
