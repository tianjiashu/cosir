"""文件文本分段投影；与领域 TextPage 对应。"""

from pydantic import BaseModel

from app.service.workspace_files.file_models import TextPage


class TextResponse(BaseModel):
    text: str
    offset: int
    next_offset: int | None
    size: int
    version: str
    encoding: str

    @classmethod
    def from_domain(cls, page: TextPage) -> "TextResponse":
        return cls(
            text=page.text,
            offset=page.offset,
            next_offset=page.next_offset,
            size=page.size,
            version=page.version,
            encoding=page.encoding,
        )
