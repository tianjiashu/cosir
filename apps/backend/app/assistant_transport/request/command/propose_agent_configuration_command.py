"""按需生成子 Agent 配置草稿的 Assistant Transport 命令。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProposeAgentConfigurationCommand(BaseModel):
    """声明当前消息需要临时开放配置提案工具。

    该命令没有 payload、版本或兼容性字段；它只表达本次 Run 的能力意图，真正的草稿仍由
    模型调用工具生成，配置保存也不在此命令内发生。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["propose-agent-configuration"]

