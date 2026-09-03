"""Windows 目录 reparse point 判定。"""

from __future__ import annotations

import os
import stat


def is_windows_directory_reparse_point(path: os.PathLike[str]) -> bool:
    """判断目录项是否为 Windows 目录 reparse point（含 junction）。

    参数:
        path: 待检查目录项。

    返回:
        Windows 下目录项带 ``FILE_ATTRIBUTE_REPARSE_POINT`` 时返回 True。

    异常:
        无。lstat 或目录判断失败时返回 False。

    副作用:
        仅读取目录项元数据。
    """

    if os.name != "nt":
        return False
    try:
        metadata = os.lstat(path)
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        directory_flag = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x0010)
        return bool(attributes & reparse_flag) and bool(attributes & directory_flag)
    except OSError:
        return False
