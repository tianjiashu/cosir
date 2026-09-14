"""搜索 engine 输出的结构化结果。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchMatch:
    """一行搜索结果；``is_match`` 区分命中行与上下文行。"""

    file_path: str
    line_number: int
    content: str
    is_match: bool


@dataclass(frozen=True)
class ContentSearchPage:
    """内容搜索的一页结果及扫描统计。"""

    matches: tuple[SearchMatch, ...]
    total_rows: int
    match_count: int
    scanned_files: int
    skipped_files: int


@dataclass(frozen=True)
class FileSearchMatch:
    """一个文件名匹配结果。"""

    file_path: str
    mtime: float


@dataclass(frozen=True)
class FileSearchPage:
    """文件名搜索的一页结果及总数。"""

    matches: tuple[FileSearchMatch, ...]
    total_files: int
