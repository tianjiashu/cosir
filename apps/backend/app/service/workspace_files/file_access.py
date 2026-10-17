"""打开句柄是内容读取的唯一范围边界，避免软链接在检查和打开之间被替换。"""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


def opened_path(descriptor: int) -> Path:
    """从操作系统查询已打开对象的位置，不再沿用户提供的软链接重新寻址。"""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_path = kernel.GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(descriptor)
        length = get_path(handle, None, 0, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(length + 1)
        if not get_path(handle, buffer, len(buffer), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    elif sys.platform == "darwin":
        import fcntl

        # Darwin F_GETPATH 返回该句柄的绝对路径，内核要求 MAXPATHLEN 缓冲区。
        return Path(os.fsdecode(fcntl.fcntl(descriptor, 50, bytes(1024)).split(b"\0", 1)[0]))
    else:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


@contextmanager
def regular_file(path: Path, root: Path) -> Iterator[BinaryIO]:
    """打开后、读取前只校验一次句柄范围及类型；非阻塞打开避免 FIFO 挂死线程。"""
    if sys.platform == "win32":
        flags = os.O_RDONLY | os.O_BINARY
    else:
        flags = os.O_RDONLY | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("只能预览普通文件")
        if not opened_path(descriptor).is_relative_to(root):
            raise PermissionError("链接目标位于工作区之外")
        yield source


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
        actual = opened_path(descriptor)
        if not actual.is_relative_to(root):
            raise PermissionError("链接目标位于工作区之外")
        with os.scandir(str(actual) if os.name == "nt" else descriptor) as scan:
            yield scan
    finally:
        os.close(descriptor)
