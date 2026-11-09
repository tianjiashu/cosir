"""Agent 配置创建/更新请求体。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config.configuration import get_tool_registry
from app.service.configuration.agent_configuration import AgentConfiguration


class AgentConfigurationRequest(BaseModel):
    """校验 Agent 配置的创建与更新请求体。

    参数:
        agent_id: Agent 标识，与配置文件身份一致。
        role: Agent 角色描述。
        description: Agent 用途说明。
        system_prompt: 系统提示词正文。
        allowed_tool_groups: 该 Agent 允许使用的工具分组名称列表；后端会展开为工具名。
        max_steps: 单次运行的最大推理步数，默认 100。
        model_config_id: 可选，指向数据库模型连接配置的标识。
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
    model_config_id: str | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)

    def to_configuration(self) -> AgentConfiguration:
        """转成配置服务收的配置内容，并把工具组展开为工具名。

        不存在的工具组不展开出任何工具名（不报错，也不保留组名）。

        异常:
            RuntimeError: 进程级工具注册表尚未初始化（见 ``get_tool_registry``）。
        """

        return AgentConfiguration(
            agent_id=self.agent_id,
            role=self.role,
            description=self.description,
            system_prompt=self.system_prompt,
            allowed_tools=get_tool_registry().tool_groups_to_tool_names(self.allowed_tool_groups),
            max_steps=self.max_steps,
            model_config_id=self.model_config_id,
            model_settings=self.model_settings,
        )
