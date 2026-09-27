"""Agent 配置创建/更新请求体。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentConfigurationRequest(BaseModel):
    """校验 Agent 配置的创建与更新请求体。

    参数:
        agent_id: Agent 标识，与配置文件身份一致。
        role: Agent 角色描述。
        description: Agent 用途说明。
        system_prompt: 系统提示词正文。
        allowed_tool_groups: 该 Agent 允许使用的工具分组名称列表；后端会展开为工具名。
        max_steps: 单次运行的最大推理步数，默认 100。
        provider_id: 可选，模型归属厂商标识（指向 ``providers.id``）。
        model_name: 可选，模型名；None 表示未指定模型。
        model_settings: 可选，模型参数覆盖项。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 字段类型不符或携带额外字段时由 Pydantic 校验抛出。

    副作用:
        无（只做结构校验，不读写文件）。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    role: str
    description: str
    system_prompt: str
    allowed_tool_groups: list[str]
    max_steps: int = 100
    provider_id: int | None = None
    model_name: str | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)
