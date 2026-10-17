"""目录分页投影；与领域 DirectoryPage 对应，entries 用 FileEntryResponse 投影。"""

from pydantic import BaseModel

from app.api.schemas.response.FileEntryResponse import FileEntryResponse
from app.service.workspace_files.file_models import DirectoryPage


class DirectoryResponse(BaseModel):
    path: str
    entries: list[FileEntryResponse]
    next_cursor: str | None
    total: int
    hidden_count: int = 0

    @classmethod
    def from_domain(cls, page: DirectoryPage) -> "DirectoryResponse":
        return cls(
            path=page.path,
            entries=[FileEntryResponse.from_domain(entry) for entry in page.entries],
            next_cursor=page.next_cursor,
            total=page.total,
            hidden_count=page.hidden_count,
        )
