"""主 Agent 系统提示词更新请求体。"""

from pydantic import BaseModel, ConfigDict


class MainAgentPromptUpdateRequest(BaseModel):
    """校验主 Agent prompt 更新请求的字段形状。"""

    model_config = ConfigDict(extra="forbid")

    content: str
