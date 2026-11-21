"""``session_list`` 工具的参数模型（无参数）。"""

from pydantic import BaseModel, ConfigDict


class SessionListArgs(BaseModel):
    """列出自己派生的 Session，不带参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")
