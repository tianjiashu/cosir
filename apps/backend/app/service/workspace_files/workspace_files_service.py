"""工作区文件浏览用例的门面：解析工作区根、路径与隐藏设置，再交给目录浏览、文件内容与查找各块。

API 只经这里访问文件面板；仅浏览设置可写，文件内容只读。
"""

import threading
from collections.abc import Iterator
from pathlib import Path

from app.service.depends import get_workspace_crud
from app.service.workspace_files.browser_visibility import (
    BrowserVisibility,
    BrowserVisibilityStore,
    VisibilityChange,
)
from app.service.workspace_files.content.file_content import inspect_file, read_text
from app.service.workspace_files.directory.directory_snapshots import DirectorySnapshots
from app.service.workspace_files.file_models import DirectoryPage, FileInfo, SearchBatch, TextPage
from app.service.workspace_files.search.name_search import search_names
from app.service.workspace_files.search.search_paging import file_search_pages, text_search_pages
from app.service.workspace_files.search.text_search import search_text
from app.service.workspace_files.workspace_paths import resolve_path


class WorkspaceFilesService:
    """文件读取限于根目录；浏览设置与 Agent 搜索配置独立。

    工作区记录经装配层提供的 workspace crud 单例读取，不再由构造方注入契约。
    """

    def __init__(self) -> None:
        self.workspace_crud = get_workspace_crud()
        self._snapshots = DirectorySnapshots()

    def root(self, workspace_id: int) -> Path:
        """工作区根目录。工作区记录不存在或根目录已被删除都抛 ``KeyError``：「工作区不在」与「文件不在」
        （``FileNotFoundError``）是两种结局，HTTP 边界据此给出不同的错误码，前端按码区分展示。"""
        try:
            return Path(self.workspace_crud.get(workspace_id).root_path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise KeyError(f"workspace {workspace_id} root is missing") from exc

    def path(self, workspace_id: int, relative: str) -> Path:
        return resolve_path(self.root(workspace_id), relative)

    def invalidate(self, workspace_id: int) -> None:
        self._snapshots.invalidate(workspace_id)

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
        self._snapshots.close()

    def directory(
        self,
        workspace_id: int,
        relative: str,
        cursor: str | None,
        limit: int,
        cancelled: threading.Event | None = None,
    ) -> DirectoryPage:
        root = self.root(workspace_id)
        return self._snapshots.page(
            workspace_id,
            root,
            resolve_path(root, relative),
            relative,
            BrowserVisibilityStore(root).read(),
            cursor,
            limit,
            cancelled,
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
        root = self.root(workspace_id)
        return search_names(
            root, BrowserVisibilityStore(root).read(), query, cancelled, include_directories
        )

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
