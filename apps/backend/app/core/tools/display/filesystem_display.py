"""文件系统工具的 UI 展示数据构造。

本模块只负责把已完成的文件读取、搜索、目录和删除结果投影为客户端数据；不执行
文件系统操作，也不构造模型可见的正文或错误诊断。
"""

from collections.abc import Iterable, Mapping
from typing import Any


_SEARCH_LINE_DISPLAY_LIMIT = 240


def build_read_file_display_data(
    path: str,
    *,
    start_line: int,
    end_line: int | str | None,
    file_size: int,
    truncated: bool = False,
) -> dict[str, Any]:
    """构造 read_file 的最小展示数据。

    ``end_line`` 使用 ``"END"`` 表示已经读到文件行尾；文件过大导致字符预算截断时
    才附带固定短提示。分页到达 limit 不视为文件截断。
    """

    data: dict[str, Any] = {
        "kind": "read-file-meta",
        "path": path,
        "line_range": {"start": start_line, "end": end_line},
        "file_size": file_size,
    }
    if truncated:
        data["truncated"] = True
        data["status_hint"] = "文件过大，已截断"
    return data


def build_file_search_display_data(
    *,
    pattern: str,
    path: str,
    files: Iterable[str],
    offset: int,
    limit: int,
    match_count: int,
) -> dict[str, Any]:
    """构造 find_files 的有界文件列表展示数据。"""

    has_more = match_count > offset + limit
    return {
        "kind": "file-list",
        "files": [{"path": item} for item in files],
        "pattern": pattern,
        "path": path,
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
        "match_count": match_count,
    }


def build_content_search_display_data(
    *,
    pattern: str,
    path: str,
    matches: Iterable[Any],
    offset: int,
    limit: int,
    total_rows: int,
    match_count: int,
    scanned_files: int,
    skipped_files: int,
) -> dict[str, Any]:
    """构造 content-search-results 展示数据，不解析模型正文。"""

    rows = [
        {
            "path": item.file_path,
            "line": item.line_number,
            "content": _bounded_search_line(item.content),
            "is_match": item.is_match,
        }
        for item in matches
    ]
    has_more = total_rows > offset + limit
    return {
        "kind": "content-search-results",
        "pattern": pattern,
        "path": path,
        "matches": rows,
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
        "total_rows": total_rows,
        "match_count": match_count,
        "scanned_files": scanned_files,
        "skipped_files": skipped_files,
    }


def _bounded_search_line(value: str) -> str:
    """限制 UI 命中行长度，避免 display_data 携带超长源码行。"""

    if len(value) <= _SEARCH_LINE_DISPLAY_LIMIT:
        return value
    return value[:_SEARCH_LINE_DISPLAY_LIMIT] + "…"


def build_directory_display_data(
    *,
    path: str,
    entries: Iterable[Mapping[str, Any]],
    offset: int,
    limit: int,
    total_entries: int,
    next_offset: int | None,
) -> dict[str, Any]:
    """构造 list_directory 的目录条目展示数据。"""

    return {
        "kind": "directory-list",
        "path": path,
        "entries": [dict(entry) for entry in entries],
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": next_offset is not None,
            "next_offset": next_offset,
        },
        "total_entries": total_entries,
    }


def build_repeated_call_display_data(*, unchanged: bool = False) -> dict[str, Any]:
    """构造文件工具重复调用的短展示标记。"""

    return (
        {"kind": "repeated-call", "unchanged": True}
        if unchanged
        else {"kind": "repeated-call", "repeated": True, "warning": True}
    )
