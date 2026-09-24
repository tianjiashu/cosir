"""把用户路径解析成可供搜索 engine 消费的文件或目录范围。"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.tools.tool_handler.search.errors import SearchPathNotFound, SearchPathUnreadable
from app.core.tools.tool_handler.search.file_walker import iter_files
from app.core.tools.tool_handler.search.ignore_rules import load_ignore_rules

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
        """产生 scope 内候选文件，不做内容读取。

        目录 scope 按当前 workspace 的 ``.cosir/.fileignore`` 规则跳过目录；规则在每次调用时
        重新读取，因此用户改动规则无需重启即可生效。文件 scope 不遍历目录，规则不参与。

        参数:
            file_glob: 可选文件名 glob；None 表示不过滤。

        返回:
            候选文件路径生成器（不保证顺序）。

        异常:
            无：规则文件读取失败由 ``load_ignore_rules`` 内部降级为默认规则。

        副作用:
            规则文件缺失时可能创建 ``<workspace>/.cosir/.fileignore``（由 ``load_ignore_rules``
            承担）；目录遍历本身只读。
        """

        if self.kind == "file":
            if file_glob is None or fnmatch.fnmatchcase(self.path.name, file_glob):
                yield self.path
            return
        yield from iter_files(self.path, file_glob, rules=load_ignore_rules(self.workspace_root))

    def display_path(self, file_path: Path) -> str:
        """生成稳定的模型可用路径；workspace 内统一使用 workspace-relative。"""

        try:
            return str(file_path.relative_to(self.workspace_root)).replace("\\", "/")
        except ValueError:
            return str(file_path).replace("\\", "/")
