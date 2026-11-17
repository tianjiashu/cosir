"""工作区文件访问边界：保留展示路径；范围与链接环都按文件身份判定（``file_identity``），不比较解析后的路径字符串
——macOS 的 ``realpath`` 不统一大小写与 firmlink，同一个目录可能有几种写法。"""

import os
import re
from pathlib import Path, PurePosixPath

from app.service.workspace_files.file_models import FileEntry
from app.tools.tool_handler.security.windows_reparse_point import (
    is_windows_directory_reparse_point,
)
from app.utils.path.file_identity import FileIdentity, RootDirectory
from app.utils.path.relative_path import has_nul, has_parent_segment


def resolve_path(root: Path, relative: str) -> Path:
    """拒绝绝对路径与越界链接；调用方传入已解析的 workspace 根。"""
    if (
        has_nul(relative)
        or PurePosixPath(relative).is_absolute()
        or has_parent_segment(relative)
    ):
        raise ValueError("路径必须相对于工作区")
    if os.name == "nt" and ("\\" in relative or ":" in relative):
        raise ValueError("路径必须使用工作区相对路径")
    target = root / relative
    resolved = target.resolve(strict=True)
    if not RootDirectory.at(root).contains(resolved):
        raise PermissionError("链接目标位于工作区之外")
    return target


def entry_for(root: Path, path: Path, ancestors: set[FileIdentity]) -> FileEntry:
    """条目自身不可读仍保留名称；链接到祖先时阻止继续展开。

    只有链接条目要判范围：普通子项就在已校验过的目录里，逐个 ``stat`` 祖先没有必要。
    """
    relative = path.relative_to(root).as_posix()
    linked = path.is_symlink() or is_windows_directory_reparse_point(path)
    try:
        resolved = path.resolve(strict=True)
        if linked and not RootDirectory.at(root).contains(resolved):
            return FileEntry(relative, path.name, "unavailable", linked, "链接目标位于工作区之外")
        if path.is_dir():
            if FileIdentity.of(resolved) in ancestors:
                return FileEntry(relative, path.name, "unavailable", linked, "目录链接形成循环")
            return FileEntry(relative, path.name, "directory", linked)
        if path.is_file():
            return FileEntry(relative, path.name, "file", linked)
        return FileEntry(relative, path.name, "unavailable", linked, "不是普通文件")
    except (OSError, RuntimeError):
        return FileEntry(relative, path.name, "unavailable", linked, "无法访问或链接已失效")


def ancestor_identities(root: Path, path: Path) -> set[FileIdentity]:
    """按展示路径的父链取各层目录的身份：既防链接环，也允许不同路径指向同一内部文件夹。"""
    current = path
    result = {FileIdentity.of(root)}
    while current != root:
        result.add(FileIdentity.of(current))
        current = current.parent
    return result


def natural_key(entry: FileEntry) -> tuple[bool, list[tuple[int, str | int]], str]:
    parts = [
        (1, int(p)) if p.isdecimal() else (0, p.casefold()) for p in re.split(r"(\d+)", entry.name)
    ]
    return entry.kind != "directory", parts, entry.name
