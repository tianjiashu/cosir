"""按需开放 Agent Team 配置提案工具的 Assistant Transport 命令。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProposeAgentTeamConfigurationCommand(BaseModel):
    """声明当前消息需要临时开放 Team 配置提案工具。

    命令只记录本轮能力意图；草稿由工具生成，保存由用户在配置编辑器中显式触发。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["propose-agent-team-configuration"]
