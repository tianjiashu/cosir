"""Agent 配置响应结构。"""

from typing import Any

from pydantic import BaseModel, Field

from app.config.configuration import get_tool_registry
from app.service.configuration.agent_configuration import AgentConfigurationEntry


class AgentConfigurationResponse(BaseModel):
    """校验并序列化 Agent 配置响应。

    参数:
        agent_id: Agent 标识。
        role: Agent 角色描述。
        description: Agent 用途说明。
        system_prompt: 系统提示词正文。
        allowed_tool_groups: 允许使用的工具分组名称列表。
        max_steps: 单次运行的最大推理步数。
        model_config_id: 可选，数据库模型连接配置标识。
        model_settings: 模型参数覆盖项。
        source: 配置来源（内置或用户文件）。
        path: 配置文件路径；无文件时为 None。
        editable: 是否可编辑。
        deletable: 是否可删除。
        validation_status: 配置校验状态。
        validation_error: 校验失败原因；通过时为 None。
        file_name: 配置文件名；缺失时为 None。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    description: str
    system_prompt: str
    allowed_tool_groups: list[str]
    max_steps: int
    model_config_id: int | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)
    source: str
    path: str | None = None
    editable: bool
    deletable: bool
    validation_status: str
    validation_error: str | None = None
    file_name: str | None = None

    @staticmethod
    def from_entry(entry: AgentConfigurationEntry) -> "AgentConfigurationResponse":
        """把配置条目投影为 HTTP 响应：工具名换回工具组，按是否内置给出来源与可编辑性。

        ``validation_status`` / ``validation_error`` 恒为 ``valid`` / ``None``：无效的配置文件在装载
        时就被跳过，不会成为条目。
        """

        configuration = entry.configuration
        return AgentConfigurationResponse(
            agent_id=configuration.agent_id,
            role=configuration.role,
            description=configuration.description,
            system_prompt=configuration.system_prompt,
            allowed_tool_groups=get_tool_registry().tool_names_to_tool_groups(
                configuration.allowed_tools
            ),
            max_steps=configuration.max_steps,
            model_config_id=configuration.model_config_id,
            model_settings=configuration.model_settings,
            source="builtin" if entry.builtin else "user_file",
            path=str(entry.path) if entry.path else None,
            editable=not entry.builtin,
            deletable=not entry.builtin,
            validation_status="valid",
            validation_error=None,
            file_name=entry.path.name if entry.path else None,
        )
