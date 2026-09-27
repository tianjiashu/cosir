"""配置中心共享的 Agent 配置文档。

配置中心读写两侧共用同一份文档结构：API 层由请求体构造它
（:meth:`AgentConfigurationDocument.from_payload`），``AgentConfigurationService`` 由注册表 profile
投影出它，``AgentConfigurationResponse`` 再把它投影为 HTTP 响应。放在 ``app.api.schemas`` 是因为它
承载的是面向前端配置界面的传输结构（``source`` / ``editable`` / ``deletable`` /
``validation_status`` 等展示元数据），不是领域实体。

本模块只定义文档结构与纯转换：不读写文件、不访问 profile 注册表（``from_payload`` 只调用工具注册表
做「工具组 → 工具名」展开）、不做 Agent profile 校验（由 ``parse_agent_profile_document`` 负责）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest
from app.config.configuration import get_tool_registry
from app.core.agents.agent_profile import AgentProfile


@dataclass
class AgentConfigurationDocument:
    """配置中心读写共用的 Agent 配置文档及其来源状态。

    字段分两组：与 ``AgentConfigurationRequest`` 对齐的业务字段（``agent_id`` 至
    ``model_settings``），其余为 service 投影出的展示元数据——``source``（``builtin`` /
    ``user_file``）、``path`` 与 ``file_name``（无对应文件时为 ``None``）、``editable`` /
    ``deletable``（内置 Agent 不可删除）、``validation_status`` / ``validation_error``（当前投影
    恒为 ``valid`` / ``None``）。

    不负责：文件读写、注册表同步与 profile 校验（由 ``AgentConfigurationService`` 承担）。
    """

    agent_id: str
    role: str = ""
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = AgentProfile.max_steps
    provider_id: int | None = None
    model_name: str | None = None
    model_settings: dict[str, Any] = field(default_factory=dict)
    source: str = "user_file"
    path: Path | None = None
    editable: bool = True
    deletable: bool = True
    validation_status: str = "valid"
    validation_error: str | None = None
    file_name: str | None = None

    def to_json_document(self) -> dict[str, Any]:
        """返回不含 service 元数据的完整 Agent JSON 文档。"""

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "allowed_tools": list(self.allowed_tools),
            "max_steps": self.max_steps,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "model_settings": dict(self.model_settings),
        }

    @classmethod
    def from_payload(cls, payload: AgentConfigurationRequest) -> AgentConfigurationDocument:
        """由 HTTP 请求体构造配置文档，并把工具组展开为工具名。

        参数:
            payload: 已通过 Pydantic 校验的创建/更新请求体。

        返回:
            ``allowed_tools`` 为请求工具组展开结果、其余字段照抄请求体的文档；不存在的工具组不会
            展开出任何工具名（不报错，也不在文档里保留该组名）。

        异常:
            RuntimeError: 进程级工具注册表尚未初始化（见 ``get_tool_registry``）。

        副作用:
            无；只读进程内工具注册表。
        """

        allowed_tools = get_tool_registry().tool_groups_to_tool_names(payload.allowed_tool_groups)
        return cls(
            agent_id=payload.agent_id,
            role=payload.role,
            description=payload.description,
            system_prompt=payload.system_prompt,
            allowed_tools=allowed_tools,
            max_steps=payload.max_steps,
            provider_id=payload.provider_id,
            model_name=payload.model_name,
            model_settings=payload.model_settings,
        )
