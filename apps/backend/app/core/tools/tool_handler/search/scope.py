"""把用户路径解析成可供搜索 engine 消费的文件或目录范围。"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.tools.tool_handler.search.errors import SearchPathNotFound, SearchPathUnreadable
from app.core.tools.tool_handler.search.file_walker import iter_files

ScopeKind = Literal["file", "directory"]


@dataclass(frozen=True)
class SearchScope:
    """统一表示单文件或递归目录搜索范围。

    ``path`` 是已经通过 PathResolver 安全解析的绝对路径。目录 scope 递归遍历，
    文件 scope 只产生自身一个 candidate；engine 不需要再次判断路径类型。
    """

    workspace_root: Path
    path: Path
    kind: ScopeKind

    @classmethod
    def from_path(cls, workspace_root: Path, path: Path) -> SearchScope:
        """从已解析路径创建 scope，并拒绝不存在或不可搜索的目录项。"""

        try:
            if path.is_file():
                return cls(workspace_root, path, "file")
            if path.is_dir():
                return cls(workspace_root, path, "directory")
        except OSError as exc:
            raise SearchPathUnreadable(str(path)) from exc
        raise SearchPathNotFound(str(path))

    def iter_files(self, file_glob: str | None = None) -> Iterator[Path]:
        """产生 scope 内候选文件，不做内容读取。"""

        if self.kind == "file":
            if file_glob is None or fnmatch.fnmatchcase(self.path.name, file_glob):
                yield self.path
            return
        yield from iter_files(self.path, file_glob)

    def display_path(self, file_path: Path) -> str:
        """生成稳定的模型可用路径；workspace 内统一使用 workspace-relative。"""

        try:
            return str(file_path.relative_to(self.workspace_root)).replace("\\", "/")
        except ValueError:
            return str(file_path).replace("\\", "/")
