"""本机 Skill 发现与按需加载参数。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillArgs(BaseModel):
    """列出可用 Skill，或加载一个 Skill 的说明及配套资源。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "load"] = Field(
        description="'load' reads a skill or one of its files; 'list' re-reads the available skills."
    )
    skill_name: str | None = Field(
        default=None,
        description="Exact skill name as listed; required for 'load'.",
    )
    resource_path: str | None = Field(
        default=None,
        description=(
            "A file the skill refers to, relative to the skill directory (e.g. "
            "references/checklist.md); omit to load SKILL.md."
        ),
    )
