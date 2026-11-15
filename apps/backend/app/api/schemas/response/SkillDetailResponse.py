"""Skill 详情响应。"""

from pydantic import BaseModel

from app.api.schemas.response.SkillInfoResponse import SkillInfoResponse


class SkillFileResponse(BaseModel):
    """Skill 目录里的一个文件；``path`` 相对 Skill 目录，``binary`` 的文件不提供预览。"""

    path: str
    size: int
    binary: bool


class SkillDetailResponse(SkillInfoResponse):
    """Skill 详情：正文、其余 frontmatter 字段、文件清单与位置。

    ``location`` 给人看（``~/…``）；``directory`` 是真实目录的绝对路径，只给桌面宿主的「在文件夹中显示」。
    ``also_at`` 是同一份 Skill（内容相同）的其他位置，多为软链或复制安装，列表里只列生效的这一条。
    ``files_truncated`` 表示文件超过清单上限、只列出了前一部分。
    """

    content: str
    location: str
    also_at: list[str]
    directory: str
    metadata: dict[str, str]
    files: list[SkillFileResponse]
    files_truncated: bool


class SkillFileContentResponse(BaseModel):
    """Skill 目录内一个文本文件的内容（只读预览）。"""

    path: str
    content: str
