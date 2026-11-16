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
    # 位图的原图尺寸（只读文件头）；资源接口可能返回缩小图，面板据此显示真实尺寸、判断是否缩小。
    # 非位图为 None；位图读不出（像素超出 Pillow 安全上限、文件头损坏）同样为 None，前端据此直接判为无法预览。
    width: int | None
    height: int | None


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
