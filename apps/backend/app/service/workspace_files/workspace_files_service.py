"""工作区文件浏览用例；仅浏览设置可写，文件内容只读，目录快照是有界缓存。"""

import fnmatch
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.service.depends import get_workspace_crud
from app.service.workspace_files.browser_visibility import (
    BrowserVisibility,
    BrowserVisibilityStore,
    VisibilityChange,
)
from app.service.workspace_files.directory_index import DirectoryIndex
from app.service.workspace_files.file_access import directory_entries
from app.service.workspace_files.file_content import inspect_file, read_text, version_of
from app.service.workspace_files.file_models import (
    DirectoryPage,
    FileConflictError,
    FileEntry,
    FileInfo,
    SearchBatch,
    TextPage,
)
from app.service.workspace_files.search_paging import file_search_pages, text_search_pages
from app.service.workspace_files.text_search import search_text
from app.service.workspace_files.workspace_paths import ancestor_paths, entry_for, resolve_path


@dataclass
class DirectorySnapshot:
    workspace_id: int
    path: str
    version: str
    entries: DirectoryIndex
    created: float
    visibility_revision: str
    hidden_count: int


class WorkspaceFilesService:
    """文件读取限于根目录；浏览设置与 Agent 搜索配置独立。

    工作区记录经装配层提供的 workspace crud 单例读取，不再由构造方注入契约。
    """

    def __init__(self) -> None:
        self.workspace_crud = get_workspace_crud()
        self._snapshots: OrderedDict[str, DirectorySnapshot] = OrderedDict()
        self._lock = threading.Lock()

    def root(self, workspace_id: int) -> Path:
        return Path(self.workspace_crud.get(workspace_id).root_path).resolve(strict=True)

    def path(self, workspace_id: int, relative: str) -> Path:
        return resolve_path(self.root(workspace_id), relative)

    def invalidate(self, workspace_id: int) -> None:
        with self._lock:
            for token in list(self._snapshots):
                if self._snapshots[token].workspace_id == workspace_id:
                    self._snapshots.pop(token).entries.close()

    def visibility(self, workspace_id: int) -> BrowserVisibility:
        return BrowserVisibilityStore(self.root(workspace_id)).read()

    def change_visibility(
        self, workspace_id: int, paths: list[str], *, ignored: bool
    ) -> VisibilityChange:
        change = BrowserVisibilityStore(self.root(workspace_id)).change(paths, ignored=ignored)
        if change.changed_paths:
            self.invalidate(workspace_id)
        return change

    def close(self) -> None:
        with self._lock:
            for snapshot in self._snapshots.values():
                snapshot.entries.close()
            self._snapshots.clear()

    def directory(
        self,
        workspace_id: int,
        relative: str,
        cursor: str | None,
        limit: int,
        cancelled: threading.Event | None = None,
    ) -> DirectoryPage:
        root = self.root(workspace_id)
        visibility = BrowserVisibilityStore(root).read()
        path = resolve_path(root, relative)
        version = version_of(path.stat())
        if cursor:
            try:
                token, position = cursor.rsplit(":", 1)
                offset = int(position)
            except ValueError as exc:
                raise ValueError("目录游标无效") from exc
            with self._lock:
                try:
                    snapshot = self._snapshots[token]
                except KeyError as exc:
                    raise FileConflictError("目录分页已过期，请刷新") from exc
            if (
                snapshot.workspace_id != workspace_id
                or snapshot.path != relative
                or snapshot.visibility_revision != visibility.revision
                or snapshot.version != version
                or time.monotonic() - snapshot.created > 120
            ):
                raise FileConflictError("目录已变化，请刷新")
            if offset < 0:
                raise ValueError("目录游标无效")
        else:
            start = time.monotonic()
            ancestors = ancestor_paths(root, path)

            hidden_count = 0

            def iter_entries() -> Iterator[FileEntry]:
                nonlocal hidden_count
                if visibility.hides(relative):
                    hidden_count = 1
                    return
                with directory_entries(path, root) as scan:
                    for item in scan:
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("目录请求已取消")
                        child = path / item.name
                        if visibility.hides(child.relative_to(root).as_posix()):
                            hidden_count += 1
                            continue
                        yield entry_for(root, child, ancestors)

            entries = DirectoryIndex(iter_entries(), cancelled)
            if cancelled is not None and cancelled.is_set():
                entries.close()
                raise InterruptedError("目录请求已取消")
            if version_of(path.stat()) != version:
                entries.close()
                raise FileConflictError("目录扫描期间发生变化，请重试")
            snapshot = DirectorySnapshot(
                workspace_id,
                relative,
                version,
                entries,
                time.monotonic(),
                visibility.revision,
                hidden_count,
            )
            token, offset = uuid.uuid4().hex, 0
            with self._lock:
                self._snapshots[token] = snapshot
                for old_token in list(self._snapshots):
                    if time.monotonic() - self._snapshots[old_token].created > 120:
                        self._snapshots.pop(old_token).entries.close()
                while len(self._snapshots) > 16:
                    self._snapshots.popitem(last=False)[1].entries.close()
            log.info(
                "workspace_directory_loaded",
                extra={
                    "msg": "目录读取完成",
                    "data": {
                        "workspace_id": workspace_id,
                        "path": relative,
                        "entries": entries.total,
                        "duration_ms": round((time.monotonic() - start) * 1000),
                    },
                },
            )
        end = offset + limit
        with self._lock:
            if token not in self._snapshots:
                raise FileConflictError("目录已变化，请刷新")
            return DirectoryPage(
                relative,
                snapshot.entries.page(offset, limit),
                f"{token}:{end}" if end < snapshot.entries.total else None,
                snapshot.entries.total,
                snapshot.hidden_count,
            )

    def info(self, workspace_id: int, relative: str) -> FileInfo:
        root = self.root(workspace_id)
        return inspect_file(root, resolve_path(root, relative))

    def text(
        self,
        workspace_id: int,
        relative: str,
        offset: int,
        encoding: str,
        version: str | None,
        tail: bool,
        before: int | None = None,
        cancelled: threading.Event | None = None,
    ) -> TextPage:
        root = self.root(workspace_id)
        return read_text(
            resolve_path(root, relative),
            offset,
            encoding,
            version,
            tail,
            root=root,
            before=before,
            cancelled=cancelled,
        )

    def search(
        self,
        workspace_id: int,
        query: str,
        cancelled: threading.Event,
        include_directories: bool = False,
    ) -> Iterator[SearchBatch]:
        """按展示路径遍历并流式返回；每条祖先链独立检测链接环，取消不等整树结束。"""
        root = self.root(workspace_id)
        visibility = BrowserVisibilityStore(root).read()
        stack = [(root, {root})]
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
                            stack.append((path, ancestors | {path.resolve(strict=True)}))
                        elif entry.kind == "unavailable":
                            skipped += 1
                            reason = entry.hint or "条目不可读取"
                            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                        if entry.kind == "file" or (
                            include_directories and entry.kind == "directory"
                        ):
                            haystack = relative if "/" in query else path.name
                            if (
                                fnmatch.fnmatchcase(haystack.casefold(), pattern)
                                if glob
                                else pattern in haystack.casefold()
                            ):
                                batch.append(entry)
                        if len(batch) >= 100 or time.monotonic() - last_emit > 0.25:
                            yield SearchBatch(batch, skipped, False, dict(skipped_reasons))
                            batch, last_emit = [], time.monotonic()
            except OSError:
                skipped += 1
                reason = "目录无法读取或扫描期间已变化"
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
        if not cancelled.is_set():
            yield SearchBatch(batch, skipped, True, dict(skipped_reasons))

    def search_pages(
        self,
        workspace_id: int,
        query: str,
        include_directories: bool,
        offset: int,
        limit: int,
        cancelled: threading.Event,
    ) -> Iterator[dict[str, object]]:
        """搜索结果分页窗口由 service 统一控制偏移与上限，避免 API 层接触分页细节。"""
        return file_search_pages(
            self.search(workspace_id, query, cancelled, include_directories),
            offset,
            limit,
            cancelled,
        )

    def find_text_pages(
        self,
        workspace_id: int,
        relative: str,
        query: str,
        encoding: str,
        version: str,
        offset: int,
        limit: int,
        cancelled: threading.Event,
    ) -> Iterator[dict[str, object]]:
        """全文搜索分页窗口；相对路径与根由 service 解析，API 不再直接调用 text_search。"""
        root = self.root(workspace_id)
        path = self.path(workspace_id, relative)
        return text_search_pages(
            search_text(path, query, encoding, version, cancelled, root=root),
            offset,
            limit,
            cancelled,
        )
