"""文件元信息投影；与领域 FileInfo 对应。"""

from pydantic import BaseModel

from app.service.workspace_files.file_models import FileInfo


class FileInfoResponse(BaseModel):
    path: str
    name: str
    root_path: str
    size: int
    modified_ns: int
    version: str
    kind: str
    mime: str
    encoding: str | None

    @classmethod
    def from_domain(cls, info: FileInfo) -> "FileInfoResponse":
        return cls(
            path=info.path,
            name=info.name,
            root_path=info.root_path,
            size=info.size,
            modified_ns=info.modified_ns,
            version=info.version,
            kind=info.kind,
            mime=info.mime,
            encoding=info.encoding,
        )
