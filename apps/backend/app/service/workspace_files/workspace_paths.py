"""工作区文件访问边界：把前端给的相对路径解析为工作区内的展示路径；范围按文件身份判定（``file_identity``），
不比较解析后的路径字符串——macOS 的 ``realpath`` 不统一大小写与 firmlink，同一个目录可能有几种写法。"""

import os
from pathlib import Path, PurePosixPath

from app.utils.path.file_identity import RootDirectory
from app.utils.path.relative_path import has_nul, has_parent_segment


def resolve_path(root: Path, relative: str) -> Path:
    """拒绝绝对路径与越界链接；调用方传入已解析的 workspace 根。"""
    if has_nul(relative) or PurePosixPath(relative).is_absolute() or has_parent_segment(relative):
        raise ValueError("路径必须相对于工作区")
    if os.name == "nt" and ("\\" in relative or ":" in relative):
        raise ValueError("路径必须使用工作区相对路径")
    target = root / relative
    resolved = target.resolve(strict=True)
    if not RootDirectory.at(root).contains(resolved):
        raise PermissionError("链接目标位于工作区之外")
    return target
