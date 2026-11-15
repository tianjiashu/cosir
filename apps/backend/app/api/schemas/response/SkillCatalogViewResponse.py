"""Skill 配置页分组视图响应。"""

from typing import Literal

from pydantic import BaseModel

from app.api.schemas.response.SkillInfoResponse import SkillInfoResponse


class SkillEntryResponse(SkillInfoResponse):
    """分组里的一个 Skill：``standing`` 是它的处境，只有 ``effective`` 的条目可以启停。"""

    standing: Literal["effective", "shadowed"]


class SkippedSkillResponse(BaseModel):
    """Skill 目录里一个无法加载的子目录。"""

    directory: str
    reason: str


class SkillGroupResponse(BaseModel):
    """一个根目录（作用域 × 身份）；``location`` 是给人看的位置（如 ``~/.agents/skills``、``.cosir/skills``）。"""

    scope: Literal["system", "workspace"]
    kind: Literal["cosir", "shared"]
    location: str
    skipped: list[SkippedSkillResponse]
    skills: list[SkillEntryResponse]


class SkillCatalogViewResponse(BaseModel):
    """配置页分组目录；``agent_skills`` / ``agent_tokens`` 是 Agent 实际看到的目录条数与估算 token。"""

    agent_skills: int
    agent_tokens: int
    groups: list[SkillGroupResponse]
