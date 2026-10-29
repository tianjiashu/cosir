from pydantic import BaseModel, ConfigDict, Field


class CreateTerminalSessionRequest(BaseModel):
    """用户在终端工作台新建会话的请求体。

    ``shell`` 取 ``GET /terminal/shells`` 的 ``kind`` 或 ``auto``。

    shell 是否可用由终端服务解析时判定（不可用映射 400），这里只约束形状。
    """

    model_config = ConfigDict(extra="forbid")

    shell: str = Field(default="auto", min_length=1, max_length=64)
