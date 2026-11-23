"""文件的版本身份：由文件系统元数据判断还是不是同一个版本。"""

from __future__ import annotations

from os import stat_result
from pathlib import Path
from stat import S_ISREG

from pydantic import BaseModel, ConfigDict


class FileVersion(BaseModel):
    """由文件系统元数据构成的文件身份和版本标记。"""

    model_config = ConfigDict(frozen=True)
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: stat_result) -> FileVersion:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )

    @classmethod
    def of(cls, path: Path) -> FileVersion | None:
        """返回可访问的普通文件的版本标记，其他情况返回空值。"""
        try:
            value = path.stat()
        except OSError:
            return None
        return cls.from_stat(value) if S_ISREG(value.st_mode) else None

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode
