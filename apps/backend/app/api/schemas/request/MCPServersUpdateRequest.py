"""替换系统级 MCP 服务列表的请求体。"""

from pydantic import BaseModel, ConfigDict

from app.api.schemas.request.MCPServerConfigRequest import MCPServerConfigRequest


class MCPServersUpdateRequest(BaseModel):
    """待保存的完整 MCP 服务列表。"""

    model_config = ConfigDict(extra="forbid")

    servers: list[MCPServerConfigRequest]
