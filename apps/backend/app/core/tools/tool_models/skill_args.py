"""本机 Skill 发现与按需加载参数。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillArgs(BaseModel):
    """列出可用 Skill，或加载一个 Skill 的说明及配套资源。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "load"] = Field(
        description="Use 'list' to discover skills, or 'load' to read a skill or its resource."
    )
    skill_name: str | None = Field(
        default=None,
        description="Skill name from 'list'; required for 'load'.",
    )
    resource_path: str | None = Field(
        default=None,
        description="Path relative to the skill directory; omit to load SKILL.md.",
    )
