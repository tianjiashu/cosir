"""按文件名查找：从工作区根按展示路径遍历，流式返回命中的条目。

查询含 ``*?[`` 时按 glob 匹配，否则按子串匹配，都不区分大小写；含 ``/`` 时匹配相对路径，否则只匹配文件名。
每条祖先链独立检测链接环（同一目录经不同路径可达仍会展开），取消不等整树结束。不可读的条目与目录计入跳过
数并按原因汇总，不中断遍历。
"""

import fnmatch
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from app.service.workspace_files.browser_visibility import BrowserVisibility
from app.service.workspace_files.directory.directory_entries import directory_entries, entry_for
from app.service.workspace_files.file_models import FileEntry, SearchBatch
from app.utils.path.file_identity import FileIdentity

# 攒够这么多条或距上一批超过这么久就交出一批，前端边搜边显示。
_BATCH_ENTRIES = 100
_BATCH_SECONDS = 0.25


def search_names(
    root: Path,
    visibility: BrowserVisibility,
    query: str,
    cancelled: threading.Event,
    include_directories: bool = False,
) -> Iterator[SearchBatch]:
    """最后一批 ``complete`` 为真；被取消时不交出最后一批。"""
    stack = [(root, {FileIdentity.of(root)})]
    batch: list[FileEntry] = []
    skipped = 0
    skipped_reasons: dict[str, int] = {}
    pattern = query.casefold()
    glob = any(char in query for char in "*?[")
    last_emit = time.monotonic()
    while stack and not cancelled.is_set():
        directory, ancestors = stack.pop()
        try:
            with directory_entries(directory, root) as scan:
                for item in scan:
                    if cancelled.is_set():
                        return
                    path = directory / item.name
                    relative = path.relative_to(root).as_posix()
                    if visibility.hides(relative):
                        continue
                    entry = entry_for(root, path, ancestors)
                    if entry.kind == "directory":
                        stack.append((path, ancestors | {FileIdentity.of(path)}))
                    elif entry.kind == "unavailable":
                        skipped += 1
                        reason = entry.hint or "条目不可读取"
                        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    if entry.kind == "file" or (include_directories and entry.kind == "directory"):
                        haystack = relative if "/" in query else path.name
                        if (
                            fnmatch.fnmatchcase(haystack.casefold(), pattern)
                            if glob
                            else pattern in haystack.casefold()
                        ):
                            batch.append(entry)
                    if (
                        len(batch) >= _BATCH_ENTRIES
                        or time.monotonic() - last_emit > _BATCH_SECONDS
                    ):
                        yield SearchBatch(batch, skipped, False, dict(skipped_reasons))
                        batch, last_emit = [], time.monotonic()
        except OSError:
            skipped += 1
            reason = "目录无法读取或扫描期间已变化"
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
    if not cancelled.is_set():
        yield SearchBatch(batch, skipped, True, dict(skipped_reasons))
