"""路径校验：判断某路径（含符号链接解析后）是否落在某个目录子树内。"""

from __future__ import annotations

import os
from pathlib import Path

from app.utils.path.workspace_cosir import workspace_cosir_dir


def is_within_cosir(path: str | Path, workspace_root: str | Path | None) -> bool:
    """判断路径（含符号链接解析后）是否落在 workspace ``.cosir`` 保留子树内。

    与写工具 ``PathResolver`` 的 workspace containment 规则互补：containment 保证「不出工作区」，
    本函数保证「不进入 ``.cosir`` 保留区」。比较规则见 :func:`is_within`；``workspace_root`` 为
    ``None`` 或空串时视为无 workspace，返回 ``False``。
    """

    if not workspace_root:
        return False
    return is_within(path, workspace_cosir_dir(workspace_root))


def is_within(path: str | Path, root: str | Path) -> bool:
    """判断路径（含符号链接解析后）是否等于 ``root`` 或位于其下。

    两边都取 ``realpath`` + ``normcase`` 后按**路径分量**比较，因此（a）指向 ``root`` 内的符号链接
    会命中，（b）大小写不敏感文件系统按真实大小写命中，（c）同前缀的兄弟目录（``.cosir2``）不会
    误命中。``path`` 允许尚不存在（``realpath`` 只解析已存在的祖先）；空串或纯空白视为无效路径，
    返回 ``False``（避免落到 ``realpath("")`` 的当前目录语义）。``OSError`` / ``ValueError``（如含
    NUL 字节）一律归一化为 ``False``，不向调用方抛出。
    """

    text = str(path)
    if not text.strip():
        return False
    try:
        root_text = os.path.normcase(os.path.realpath(str(root)))
        candidate = os.path.normcase(os.path.realpath(text))
    except (OSError, ValueError):
        return False
    return candidate == root_text or candidate.startswith(root_text.rstrip(os.sep) + os.sep)


__all__ = ["is_within", "is_within_cosir"]
