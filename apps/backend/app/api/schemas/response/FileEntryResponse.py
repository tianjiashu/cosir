"""目录条目投影；字段与领域 FileEntry 一致，供 API 响应契约持有。"""

from pydantic import BaseModel

from app.service.workspace_files.file_models import FileEntry


class FileEntryResponse(BaseModel):
    path: str
    name: str
    kind: str
    symlink: bool = False
    hint: str | None = None

    @classmethod
    def from_domain(cls, entry: FileEntry) -> "FileEntryResponse":
        return cls(
            path=entry.path,
            name=entry.name,
            kind=entry.kind,
            symlink=entry.symlink,
            hint=entry.hint,
        )
