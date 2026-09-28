"""Agent 配置响应结构。"""

from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.config.configuration import get_tool_registry


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
    def from_document(
        document: AgentConfigurationDocument,
    ) -> "AgentConfigurationResponse":
        """把 Agent 配置文档投影为 HTTP 响应 schema。

        参数:
            document: 配置服务返回的 Agent 配置文档。

        返回:
            可直接作为响应体返回的 ``AgentConfigurationResponse``；工具名由配置 service
            提供的 profile 转换为调用方可见的工具组，``path`` 缺失时投影为 ``None``，

        异常:
            无（字段缺失或类型不符由 Pydantic 在构造时抛 ``ValidationError``）。

        副作用:
            无（纯投影，不读写文件或数据库）。
        """

        allowed_tool_groups = get_tool_registry().tool_names_to_tool_groups(document.allowed_tools)

        return AgentConfigurationResponse(
            agent_id=document.agent_id,
            role=document.role,
            description=document.description,
            system_prompt=document.system_prompt,
            allowed_tool_groups=allowed_tool_groups,
            max_steps=document.max_steps,
            model_config_id=document.model_config_id,
            model_settings=document.model_settings,
            source=document.source,
            path=str(document.path) if document.path else None,
            editable=document.editable,
            deletable=document.deletable,
            validation_status=document.validation_status,
            validation_error=document.validation_error,
            file_name=document.file_name,
        )
