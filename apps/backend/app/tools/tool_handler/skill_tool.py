"""Agent Skills 目录发现与按需加载工具。"""

from __future__ import annotations

import json

from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SKILL,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_SKILL
from app.service.configuration.skill_catalog import SkillCatalog, SkillLoadError
from app.service.configuration.skill_configuration import SkillConfigurationError
from app.tools.display.skill_display import (
    build_skill_content_display_data,
    build_skill_list_display_data,
)
from app.tools.tool_models.skill_args import SkillArgs


class SkillTool(HandlerBase):
    """列出当前工作区可见的 Skill（系统级与 workspace 级合并），并读取其正文或配套文本资源。"""

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
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """发现 Skill 清单，或读取所选 Skill 的说明和目录内配套资源。

        加载 ``SKILL.md`` 时正文前附 Skill 目录的绝对路径（见 ``SkillDocument.for_model``），
        正文里引用的 ``scripts/``、``references/`` 等相对路径才找得到。
        """

        catalog = SkillCatalog.visible_from(execution_context.workspace_root)
        try:
            if action == "list":
                skills = [skill for skill in catalog.list_skills() if skill.enabled]
                payload = [
                    {"name": skill.name, "description": skill.description} for skill in skills
                ]
                return tool_success(
                    self.name,
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    display_data=build_skill_list_display_data(
                        [{**item, "scope": skill.scope.kind} for item, skill in zip(payload, skills)]
                    ),
                )
            if skill_name is None:
                return tool_error(
                    self.name,
                    "skill_name is required to load a Skill. "
                    "Use action='list' to see available names, then set skill_name.",
                )
            if resource_path is None:
                document = catalog.load_enabled(skill_name)
                info, content = document.info, document.for_model()
            else:
                info, content = catalog.load_resource(skill_name, resource_path)
        except SkillConfigurationError as exc:
            return tool_error(self.name, f"{exc}. Fix the Skill config file, then retry.")
        except SkillLoadError as exc:
            return tool_error(
                self.name,
                f"{exc}. Check the Skill name, SKILL.md, and resource path, then retry.",
            )

        return tool_success(
            self.name,
            content,
            display_data=build_skill_content_display_data(
                skill_name=skill_name,
                resource_path=resource_path or "SKILL.md",
                scope=info.scope.kind,
            ),
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
        )


def build_skill_definition() -> ToolDefinition:
    """构造 Skill 的固定工具定义。"""

    return SkillTool().to_definition()
