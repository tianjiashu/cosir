"""子 Agent 配置提案工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class ProposeAgentConfigurationArgs(BaseModel):
    """描述主 Agent 生成的一份未保存子 Agent 配置。

    本模型只承载用户明确要求由 Agent 生成的四个字段；工具不会接收或生成 scope、工具组、
    步数和模型选择，这些字段留给 Workbench 中的用户配置。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        description="Stable unique identifier for the proposed child agent.",
    )
    role: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="The child agent's role or responsibility within the task.",
    )
    description: StrictStr = Field(
        min_length=1,
        max_length=2_000,
        description="Short summary of what the child agent is expected to do.",
    )
    system_prompt: StrictStr = Field(
        min_length=1,
        max_length=50_000,
        description="System prompt that defines the child agent's behavior and guardrails.",
    )
