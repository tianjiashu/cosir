"""替换一个作用域 MCP 服务列表的请求体。"""

from pydantic import BaseModel, ConfigDict

from app.service.configuration.mcp.configuration import MCPServerConfig


class MCPServersUpdateRequest(BaseModel):
    """待保存的本作用域完整服务列表（不含继承来的系统级服务）。

    单个服务直接使用领域契约 ``MCPServerConfig``：字段与校验只此一份，接入层不另抄一遍。
    """

    model_config = ConfigDict(extra="forbid")

    servers: list[MCPServerConfig]
