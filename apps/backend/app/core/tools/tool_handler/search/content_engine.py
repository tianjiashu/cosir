"""结构化内容搜索 engine。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from app.core.tools.tool_handler.search.errors import (
    InvalidSearchPattern,
    SearchCancelled,
    SearchTimedOut,
)
from app.core.tools.tool_handler.search.result import ContentSearchPage, SearchMatch
from app.core.tools.tool_handler.search.scope import SearchScope

MAX_SEARCH_LINE_BYTES = 1024 * 1024


def search_content(
    scope: SearchScope,
    pattern: str,
    *,
    file_glob: str | None = None,
    context: int = 0,
    limit: int = 50,
    offset: int = 0,
    is_cancelled: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> ContentSearchPage:
    """在文件或目录 scope 中搜索正则，并返回结构化分页结果。

    不负责路径安全、ToolObservation 或模型文本格式化。不可读/二进制文件会被跳过，
    取消和超时通过异常交给 handler 转换为统一工具状态。
    """

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise InvalidSearchPattern(str(exc)) from exc

    rows: list[SearchMatch] = []
    match_count = 0
    scanned_files = 0
    skipped_files = 0

    for file_path in scope.iter_files(file_glob):
        _check_interrupt(is_cancelled, deadline)
        try:
            lines = _read_lines(file_path, is_cancelled, deadline)
        except (OSError, UnicodeDecodeError):
            skipped_files += 1
            continue
        if lines is None:
            skipped_files += 1
            continue

        scanned_files += 1
        hits: list[int] = []
        for index, line in enumerate(lines, start=1):
            _check_interrupt(is_cancelled, deadline)
            if regex.search(line):
                hits.append(index)
        if not hits:
            continue
        match_count += len(hits)
        hit_set = set(hits)
        for hit_line in hits:
            start = max(1, hit_line - context)
            end = min(len(lines), hit_line + context)
            for line_number in range(start, end + 1):
                rows.append(
                    SearchMatch(
                        file_path=scope.display_path(file_path),
                        line_number=line_number,
                        content=lines[line_number - 1],
                        is_match=line_number in hit_set,
                    )
                )

    page = tuple(rows[offset : offset + limit])
    return ContentSearchPage(
        matches=page,
        total_rows=len(rows),
        match_count=match_count,
        scanned_files=scanned_files,
        skipped_files=skipped_files,
    )


def _read_lines(
    file_path: Path,
    is_cancelled: Callable[[], bool] | None,
    deadline: float | None,
) -> list[str] | None:
    """按行读取 UTF-8 文本；二进制或超长单行返回 None。"""

    with file_path.open("rb") as file:
        sample = file.read(4096)
        if b"\x00" in sample:
            return None
        file.seek(0)
        lines: list[str] = []
        for raw_line in file:
            _check_interrupt(is_cancelled, deadline)
            if len(raw_line) > MAX_SEARCH_LINE_BYTES:
                return None
            line = raw_line.decode("utf-8", errors="strict")
            if line.endswith("\n"):
                line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
            lines.append(line)
        return lines


def _check_interrupt(
    is_cancelled: Callable[[], bool] | None,
    deadline: float | None,
) -> None:
    """在文件边界执行线程内取消和超时检查。"""

    if is_cancelled is not None and is_cancelled():
        raise SearchCancelled()
    if deadline is not None and time.monotonic() >= deadline:
        raise SearchTimedOut()
