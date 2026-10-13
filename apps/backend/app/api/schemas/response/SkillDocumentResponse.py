"""Skill 只读详情响应。"""

from app.api.schemas.response.SkillInfoResponse import SkillInfoResponse


class SkillDocumentResponse(SkillInfoResponse):
    """Skill 只读详情与 Markdown 内容。"""

    content: str
