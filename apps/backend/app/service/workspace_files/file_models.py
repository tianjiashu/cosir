"""文件浏览的读取结果；磁盘是事实源，这些对象只用于一次读取或短期缓存。"""

from dataclasses import dataclass, field
from typing import Literal


class FileConflictError(Exception):
    """分页或分段读取所依据的版本已失效，调用方应从头读取。"""


@dataclass(frozen=True)
class FileEntry:
    path: str
    name: str
    kind: Literal["directory", "file", "unavailable"]
    symlink: bool = False
    hint: str | None = None


@dataclass(frozen=True)
class DirectoryPage:
    path: str
    entries: list[FileEntry]
    next_cursor: str | None
    total: int
    hidden_count: int = 0


@dataclass(frozen=True)
class FileInfo:
    path: str
    name: str
    root_path: str
    size: int
    modified_ns: int
    version: str
    kind: Literal["text", "markdown", "image", "svg", "pdf", "binary"]
    mime: str
    encoding: str | None


@dataclass(frozen=True)
class TextPage:
    text: str
    offset: int
    next_offset: int | None
    size: int
    version: str
    encoding: str


@dataclass(frozen=True)
class SearchBatch:
    entries: list[FileEntry]
    skipped: int
    complete: bool
    skipped_reasons: dict[str, int] = field(default_factory=dict)
