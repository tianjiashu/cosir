"""结构化文件名 glob 搜索 engine。"""

from __future__ import annotations

import time
from fnmatch import fnmatchcase

from app.core.tools.tool_handler.search.errors import SearchTimedOut
from app.core.tools.tool_handler.search.result import FileSearchMatch, FileSearchPage
from app.core.tools.tool_handler.search.scope import SearchScope


def find_files(
    scope: SearchScope,
    pattern: str,
    *,
    limit: int = 50,
    offset: int = 0,
    deadline: float | None = None,
) -> FileSearchPage:
    """在文件或目录 scope 中按 glob 查找文件名并返回结构化分页结果。"""

    matched: list[FileSearchMatch] = []
    for file_path in scope.iter_files():
        _check_deadline(deadline)
        relative = scope.display_path(file_path)
        if _matches(relative, file_path.name, pattern):
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            matched.append(FileSearchMatch(relative, mtime))

    matched.sort(key=lambda item: (item.mtime, item.file_path), reverse=True)
    return FileSearchPage(tuple(matched[offset : offset + limit]), len(matched))


def _matches(relative_path: str, file_name: str, pattern: str) -> bool:
    """匹配文件名或 workspace-relative 路径。"""

    if "/" in pattern or "\\" in pattern:
        return fnmatchcase(relative_path, pattern.replace("\\", "/"))
    return fnmatchcase(file_name, pattern)


def _check_deadline(
    deadline: float | None,
) -> None:
    """在候选文件边界执行线程内超时检查。"""

    if deadline is not None and time.monotonic() >= deadline:
        raise SearchTimedOut()
