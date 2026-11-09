"""替换一个作用域 MCP 服务列表的请求体。"""

from pydantic import BaseModel, ConfigDict

from app.api.schemas.request.MCPServerConfigRequest import MCPServerConfigRequest


class MCPServersUpdateRequest(BaseModel):
    """待保存的本作用域完整服务列表（不含继承来的系统级服务）。"""

    model_config = ConfigDict(extra="forbid")

    servers: list[MCPServerConfigRequest]
