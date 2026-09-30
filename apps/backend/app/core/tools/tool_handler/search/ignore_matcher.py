"""搜索引擎忽略匹配器协议。

单一职责：定义 ``file_walker`` 在遍历时依赖的忽略判定接口，使 ``.fileignore`` 规则
（:class:`GitignoreMatcher`）、``.gitignore`` 规则（:class:`GitignoreMatcher`）与组合器
（:class:`CompositeIgnoreMatcher`）可以互换注入，互不耦合。

目录级判定（``match_dir``）命中即跳过整棵子树；文件级判定（``match_file``）命中即跳过该文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class IgnoreMatcher(Protocol):
    """搜索引擎注入的忽略匹配器协议。"""

    def match_dir(self, directory: Path) -> bool:
        """目录是否命中忽略规则；命中则其整棵子树不遍历。"""

        ...

    def match_file(self, file: Path) -> bool:
        """文件是否命中忽略规则；命中则不产出该文件。"""

        ...
