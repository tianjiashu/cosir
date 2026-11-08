"""配置中心的子 Agent 配置值对象。

``AgentConfiguration`` 是一份 Agent JSON 文件的业务内容，``AgentConfigurationService`` 的写入入口
收它、``parse_agent_profile_document`` 按它校验；``AgentConfigurationEntry`` 是注册表里一个 Agent 的
配置事实（内容、文件位置、是否内置），供 API 投影出界面需要的展示字段。两者都不读写文件或注册表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.agents.agent_profile import AgentProfile


@dataclass
class AgentConfiguration:
    """一份子 Agent 配置文件的业务内容；``allowed_tools`` 是工具名（已从工具组展开）。"""

    agent_id: str
    role: str = ""
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = AgentProfile.max_steps
    model_config_id: int | None = None
    model_settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_profile(cls, profile: AgentProfile) -> AgentConfiguration:
        """由注册表里的 profile 还原配置内容，不重新读取文件。"""

        return cls(
            agent_id=profile.agent_id,
            role=profile.role,
            description=profile.description or "",
            system_prompt=profile.system_prompt,
            allowed_tools=list(profile.allowed_tools),
            max_steps=profile.max_steps,
            model_config_id=profile.model_config_id,
            model_settings=profile.model_settings.to_dict(),
        )

    def to_json_document(self) -> dict[str, Any]:
        """返回写入文件、交给 profile 校验的 JSON 文档。"""

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "allowed_tools": list(self.allowed_tools),
            "max_steps": self.max_steps,
            "model_config_id": self.model_config_id,
            "model_settings": dict(self.model_settings),
        }


@dataclass(frozen=True)
class AgentConfigurationEntry:
    """注册表里一个子 Agent 的配置事实。

    ``path`` 为它的配置文件（内置 Agent 没有文件，为 ``None``）；``builtin`` 的 Agent 由代码定义，
    配置中心只能读取。
    """

    configuration: AgentConfiguration
    path: Path | None
    builtin: bool
