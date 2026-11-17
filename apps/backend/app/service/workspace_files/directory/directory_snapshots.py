"""目录快照：一次目录读取扫描出的排序结果，供后续游标分页复用。

第一页扫描整个目录并排好序（``DirectoryIndex``），之后的页按游标从同一份快照里取，保证翻页期间顺序稳定、
不重复也不遗漏。目录本身、隐藏设置或快照过期任何一项变了，游标即作废（``FileConflictError``），前端从头读。
快照只是有界缓存：按创建时间过期、按数量淘汰，淘汰与失效都负责释放大目录落在系统临时目录的排序文件。
"""

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.service.workspace_files.browser_visibility import BrowserVisibility
from app.service.workspace_files.content.file_content import version_of
from app.service.workspace_files.directory.directory_entries import (
    ancestor_identities,
    directory_entries,
    entry_for,
)
from app.service.workspace_files.directory.directory_index import DirectoryIndex
from app.service.workspace_files.file_models import DirectoryPage, FileConflictError, FileEntry


@dataclass
class DirectorySnapshot:
    workspace_id: int
    path: str
    version: str
    entries: DirectoryIndex
    created: float
    visibility_revision: str
    hidden_count: int


class DirectorySnapshots:
    """进程内的目录快照缓存；全部状态在 ``_lock`` 内读写，分页可能来自不同请求线程。"""

    TTL_SECONDS = 120
    CAPACITY = 16

    def __init__(self) -> None:
        self._snapshots: OrderedDict[str, DirectorySnapshot] = OrderedDict()
        self._lock = threading.Lock()

    def page(
        self,
        workspace_id: int,
        root: Path,
        path: Path,
        relative: str,
        visibility: BrowserVisibility,
        cursor: str | None,
        limit: int,
        cancelled: threading.Event | None = None,
    ) -> DirectoryPage:
        """没有游标时扫描并建新快照，有游标时从已有快照取下一页。

        ``path`` 由调用方经 ``resolve_path`` 校验过；快照按调用方给的 ``relative`` 原文登记与比对。

        异常:
            ValueError: 游标格式无效。
            FileConflictError: 游标对应的快照已过期，或目录、隐藏设置在翻页期间变了。
            InterruptedError: 扫描期间请求被取消。
        """
        version = version_of(path.stat())
        if cursor:
            token, offset, snapshot = self._resume(
                workspace_id, relative, visibility, version, cursor
            )
        else:
            snapshot = self._scan(
                workspace_id, root, path, relative, visibility, version, cancelled
            )
            token, offset = self._store(snapshot), 0
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

    def __len__(self) -> int:
        with self._lock:
            return len(self._snapshots)

    def invalidate(self, workspace_id: int) -> None:
        with self._lock:
            for token in list(self._snapshots):
                if self._snapshots[token].workspace_id == workspace_id:
                    self._snapshots.pop(token).entries.close()

    def close(self) -> None:
        with self._lock:
            for snapshot in self._snapshots.values():
                snapshot.entries.close()
            self._snapshots.clear()

    def _resume(
        self,
        workspace_id: int,
        relative: str,
        visibility: BrowserVisibility,
        version: str,
        cursor: str,
    ) -> tuple[str, int, DirectorySnapshot]:
        # 取到的快照随返回值带出：放锁后它可能被并发淘汰，最后取页时按 token 是否还在判定作废。
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
            or time.monotonic() - snapshot.created > self.TTL_SECONDS
        ):
            raise FileConflictError("目录已变化，请刷新")
        if offset < 0:
            raise ValueError("目录游标无效")
        return token, offset, snapshot

    def _scan(
        self,
        workspace_id: int,
        root: Path,
        path: Path,
        relative: str,
        visibility: BrowserVisibility,
        version: str,
        cancelled: threading.Event | None,
    ) -> DirectorySnapshot:
        start = time.monotonic()
        ancestors = ancestor_identities(root, path)
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
        return DirectorySnapshot(
            workspace_id,
            relative,
            version,
            entries,
            time.monotonic(),
            visibility.revision,
            hidden_count,
        )

    def _store(self, snapshot: DirectorySnapshot) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._snapshots[token] = snapshot
            for old_token in list(self._snapshots):
                if time.monotonic() - self._snapshots[old_token].created > self.TTL_SECONDS:
                    self._snapshots.pop(old_token).entries.close()
            while len(self._snapshots) > self.CAPACITY:
                self._snapshots.popitem(last=False)[1].entries.close()
        return token
