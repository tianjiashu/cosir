"""终端 deny-list 配置响应。"""

from pydantic import BaseModel, ConfigDict


class TerminalDenylistResponse(BaseModel):
    """向配置界面返回当前正则字符串列表。"""

    model_config = ConfigDict(extra="forbid")

    patterns: list[str]
