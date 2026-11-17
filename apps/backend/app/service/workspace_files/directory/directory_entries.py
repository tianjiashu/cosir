"""目录项：按已校验的句柄枚举目录，并把每一项判定为目录、文件或不可用。

范围与链接环都按文件身份判定（``file_identity``），不比较解析后的路径字符串——macOS 的 ``realpath``
不统一大小写与 firmlink，同一个目录可能有几种写法。打开句柄是枚举的范围边界，避免软链接在检查和打开之间被替换。
"""

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.service.workspace_files.file_models import FileEntry
from app.utils.path.file_identity import FileIdentity, RootDirectory, canonical_path_of
from app.utils.path.windows_reparse_point import is_windows_directory_reparse_point


@contextmanager
def directory_entries(path: Path, root: Path) -> Iterator[Iterator[os.DirEntry[str]]]:
    """POSIX 从已校验目录 fd 枚举；Windows 持有禁止删除共享的目录句柄再扫描其真实路径。"""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        # FILE_LIST_DIRECTORY、SHARE_READ|SHARE_WRITE、OPEN_EXISTING、BACKUP_SEMANTICS。
        # 不允许 SHARE_DELETE，保证从 handle 取得真实路径后目录不能被替换。
        handle = create(str(path), 1, 3, None, 3, 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
    try:
        actual = canonical_path_of(descriptor)
        if not RootDirectory.at(root).contains(actual):
            raise PermissionError("链接目标位于工作区之外")
        with os.scandir(str(actual) if os.name == "nt" else descriptor) as scan:
            yield scan
    finally:
        os.close(descriptor)


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
