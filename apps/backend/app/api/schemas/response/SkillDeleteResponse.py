"""Skill 删除操作响应。"""

from pydantic import BaseModel


class SkillDeleteResponse(BaseModel):
    """已删除 Skill 的结果。"""

    name: str
    deleted: bool
