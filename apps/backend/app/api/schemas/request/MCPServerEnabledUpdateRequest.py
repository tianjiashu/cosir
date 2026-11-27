"""启用或停用一个 MCP 服务的请求体。"""

from pydantic import BaseModel, ConfigDict


class MCPServerEnabledUpdateRequest(BaseModel):
    """只改启用开关：服务定义的其余部分以配置文件为准，不由界面回传。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
