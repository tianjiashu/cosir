"""Agent Skills 目录发现与按需加载工具。"""

from __future__ import annotations

import json

from app.core.tool.schemas import (
    TOOL_SKILL,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_SKILL
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.skill.skill_args import SkillArgs
from app.core.tool.handler.skill.skill_catalog import SkillCatalog, SkillLoadError
from app.core.tool.handler.skill.skill_configuration import SkillConfigurationError


class SkillTool(HandlerBase):
    """列出系统与当前工作区 Skill，并读取其正文或配套文本资源。"""

    name = TOOL_SKILL
    description = (
        "Discover and load local Agent Skills. Use action='list' to see names and descriptions, "
        "then action='load' with a relevant skill_name to read its instructions. Omit "
        "resource_path to load SKILL.md or provide a relative path to read a referenced file. "
        "Skills are read as text; scripts are not executed."
    )
    args_model = SkillArgs
    timeout_seconds = 10.0
    group = TOOL_GROUP_SKILL

    def execute(
        self,
        action: str,
        skill_name: str | None = None,
        resource_path: str | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """发现 Skill 清单，或读取所选 Skill 的说明和目录内配套资源。"""

        try:
            catalog = SkillCatalog()
        except SkillConfigurationError as exc:
            return tool_error(
                self.name,
                f"{exc}. "
                "Fix the Skill config file, then retry.",
            )
        if action == "list":
            skills = [skill for skill in catalog.list_skills() if skill.enabled]
            payload = [{"name": skill.name, "description": skill.description} for skill in skills]
            return tool_success(
                self.name,
                json.dumps(payload, ensure_ascii=False, indent=2),
                display_data={"kind": "skill-content", "action": "list", "skills": payload},
            )
        if skill_name is None:
            return tool_error(
                self.name,
                "skill_name is required to load a Skill. "
                "Use action='list' to see available names, then set skill_name.",
            )
        try:
            skill, content = catalog.load(skill_name, resource_path)
        except SkillLoadError as exc:
            return tool_error(
                self.name,
                f"{exc}. "
                "Check the Skill name, SKILL.md, and resource path, then retry.",
            )
        except FileNotFoundError:
            return tool_error(
                self.name,
                "Skill file not found. "
                "Re-list Skills or check the resource path.",
            )

        return tool_success(
            self.name,
            content,
            display_data={
                "kind": "skill-content",
                "action": "load",
                "skill_name": skill.name,
                "resource_path": resource_path or "SKILL.md",
            },
        )

    def to_definition(self) -> ToolDefinition:
        """返回可注册的 Skill 工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("skill", "filesystem"),
            display=ToolDisplayHints(
                verb="加载 Skill",
                icon="info",
                expandable=False,
                expand_layout="details",
                default_open=False,
            ),
        )


def build_skill_definition() -> ToolDefinition:
    """构造 Skill 的固定工具定义。"""

    return SkillTool().to_definition()
