"""文件系统工具的 UI 展示数据构造。

本模块只负责把已完成的文件读取、搜索、目录和删除结果投影为客户端数据；不执行
文件系统操作，也不构造模型可见的正文或错误诊断。
"""

from collections.abc import Iterable, Mapping
from typing import Any


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
    target: str,
    path: str,
    files: Iterable[str],
    offset: int,
    limit: int,
    match_count: int,
) -> dict[str, Any]:
    """构造 search_files 的有界文件列表展示数据。"""

    has_more = match_count > offset + limit
    return {
        "kind": "file-list",
        "files": [{"path": item} for item in files],
        "pattern": pattern,
        "target": target,
        "path": path,
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
        "match_count": match_count,
    }


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


def build_delete_display_data(
    *, path: str, target_type: str, recursive: bool
) -> dict[str, Any]:
    """构造 delete 成功结果的最小展示数据。"""

    return {
        "kind": "delete-result",
        "path": path,
        "target_type": target_type,
        "recursive": recursive,
    }


def build_repeated_call_display_data(*, unchanged: bool = False) -> dict[str, Any]:
    """构造文件工具重复调用的短展示标记。"""

    return (
        {"kind": "repeated-call", "unchanged": True}
        if unchanged
        else {"kind": "repeated-call", "repeated": True, "warning": True}
    )
