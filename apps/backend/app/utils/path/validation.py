"""路径校验：判断某路径是否落在 workspace ``.cosir`` 保留子树内。"""

from __future__ import annotations

import os
from pathlib import Path

from app.utils.path.workspace_cosir import workspace_cosir_dir


def is_within_cosir(path: str | Path, workspace_root: str | Path | None) -> bool:
    """判断路径（含符号链接解析后）是否落在 workspace ``.cosir`` 保留子树内。

    与写工具 ``PathResolver`` 的 workspace containment 规则互补：containment 保证「不出工作区」，
    本函数保证「不进入 ``.cosir`` 保留区」。判定在 ``realpath`` + ``normcase`` 之后按**路径分量**
    比较，因此（a）指向 ``.cosir`` 的符号链接会命中，（b）大小写不敏感文件系统按真实大小写命中，
    （c）``.cosir2`` 这类前缀相同但不同名的兄弟目录不会误命中。

    参数:
        path: 待判断的路径；允许尚不存在的写目标（``realpath`` 只解析已存在的祖先）。空串或
            纯空白视为无效路径，直接返回 ``False``（避免落到 ``realpath("")`` 的当前目录语义）。
        workspace_root: 工作区根路径；为 ``None`` 或空串时视为无 workspace，直接返回 ``False``。

    返回:
        ``True`` 表示 ``path`` 等于 ``<workspace_root>/.cosir`` 或位于其下；否则 ``False``。

    异常:
        无：``OSError`` 与 ``ValueError``（路径非法或无法解析，例如含 NUL 字节的路径）一律
        归一化为 ``False``，不向调用方抛出。

    副作用:
        无（只解析路径字符串，不读写文件）。
    """

    text = str(path)
    if not workspace_root or not text.strip():
        return False
    try:
        cosir_root = os.path.normcase(os.path.realpath(str(workspace_cosir_dir(workspace_root))))
        candidate = os.path.normcase(os.path.realpath(text))
    except (OSError, ValueError):
        return False
    return candidate == cosir_root or candidate.startswith(cosir_root + os.sep)


__all__ = ["is_within_cosir"]
