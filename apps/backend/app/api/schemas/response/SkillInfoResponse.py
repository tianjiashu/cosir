"""系统 Skill 列表项响应。"""

from pydantic import BaseModel


class SkillInfoResponse(BaseModel):
    """系统 Skill 列表项。"""

    name: str
    description: str
    enabled: bool
