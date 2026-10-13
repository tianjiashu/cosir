"""更新系统 Skill 启用名单的请求体。"""

from pydantic import BaseModel, ConfigDict


class SkillsEnabledUpdateRequest(BaseModel):
    """需要启用的 Skill 名称集合。"""

    model_config = ConfigDict(extra="forbid")

    enabled_skills: list[str]
