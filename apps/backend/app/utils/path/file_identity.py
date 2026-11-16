"""路径身份：「两个路径是不是同一个文件」「一个路径在不在某个根之下」的唯一判定，以及显示用的规范路径。

为什么不比字符串：macOS 默认文件系统不区分大小写，``realpath`` / ``Path.resolve`` 却保留输入的大小写与
``/System/Volumes/Data`` firmlink 前缀，``os.path.normcase`` 在 POSIX 上什么都不做，所以 ``~/.COSIR/.env``
与 ``~/.cosir/.env`` 对系统是同一个文件、对字符串比较是两个（受保护路径曾因此可被绕过）。Windows 还有 8.3
短名、``\\\\?\\`` 前缀、盘符大小写。这里改为比文件身份 ``(st_dev, st_ino)``：由操作系统解析出它到底是哪个对象，
大小写、firmlink、NFC/NFD、短名、junction 都不再参与判定，也不按平台分支。

两个原语、职责分开：

- :class:`RootDirectory` 回答「在不在根之下」：先 ``realpath`` 消掉 ``..`` 并解析全部链接（尾部不存在的
  部分按字面保留），再从结果的叶子往上逐级 ``stat``，第一个身份等于根的祖先之下即在根内。必须先解析再比：
  对原始输入直接逐级比，根内指向外部的链接与 ``ws/../out/x`` 都会被误判在内。只 ``stat`` 不打开目录，
  没有读权限的目录也能判，也不会比现在更早触发 macOS 的目录授权弹框。
- :func:`canonical_path` / :func:`canonical_path_of` 回答「这个对象在系统看来叫什么」，只用于显示与记录，
  不拿来做包含判定。

信任边界上的包含判定一律用 :class:`RootDirectory`；纯展示的相对化（输入来自已校验的遍历）不必经过这里。
"""

from __future__ import annotations

import errno
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath


@dataclass(frozen=True)
class FileIdentity:
    """一个文件系统对象的身份：所在卷与卷内文件号。``os.stat`` 跟随链接，所以链接与目标同一身份。"""

    device: int
    inode: int

    @classmethod
    def of(cls, path: str | Path) -> FileIdentity:
        """取路径（跟随链接后）的身份；不存在或无权访问时 ``OSError`` 上浮。"""

        stat = os.stat(path)
        return cls(stat.st_dev, stat.st_ino)


class UnsupportedFileSystemError(OSError):
    """根目录所在的卷不提供文件号（``st_ino`` 为 0），无法可靠判定包含关系。

    个别网络共享盘与 FAT 类卷可能如此；身份比较在这种卷上会把整个卷都判为「在根内」，所以明确拒绝，不退回
    字符串比较。添加工作区时就用它把关（``WorkspaceService``），运行中碰到同样明确失败。
    """


@dataclass(frozen=True)
class RootDirectory:
    """一个子树的根；身份只在构造时取一次，之后每次判定只 ``stat`` 候选路径的祖先。"""

    path: Path
    identity: FileIdentity

    @classmethod
    def at(cls, path: str | Path) -> RootDirectory:
        """以 ``path``（解析链接后）为根。

        异常:
            FileNotFoundError / OSError: 根不存在或无法访问。
            UnsupportedFileSystemError: 根所在的卷不提供文件号。
        """

        real = Path(os.path.realpath(path))
        identity = FileIdentity.of(real)
        if identity.inode == 0:
            raise UnsupportedFileSystemError(
                errno.ENOTSUP,
                "this disk does not report file identities, so cosir cannot reliably tell which "
                "files are inside the folder; move the project to a local disk",
                str(real),
            )
        return cls(real, identity)

    def relative(self, path: str | Path) -> PurePath | None:
        """``path`` 在根内时返回相对路径（等于根时为 ``PurePath('.')``），否则返回 None。

        ``path`` 应为绝对路径（相对路径会按进程当前目录解析）。尾部不存在的层与 ``stat`` 失败的层跳过、
        继续往上，所以写新文件也能判。返回的相对路径取自解析后的字面，只用于拼接与显示。
        """

        current = Path(os.path.realpath(path))
        tail: list[str] = []
        while True:
            try:
                if FileIdentity.of(current) == self.identity:
                    return PurePath(*reversed(tail))
            except OSError:
                pass
            if current.parent == current:
                return None
            tail.append(current.name)
            current = current.parent

    def contains(self, path: str | Path) -> bool:
        return self.relative(path) is not None


def canonical_path(path: str | Path) -> Path:
    """已存在对象在系统看来的路径（大小写取磁盘上的、firmlink 与链接都已解开），只用于显示与记录。

    macOS 打开后取 ``F_GETPATH``（``realpath`` 不统一大小写与 firmlink）；Windows 用
    ``realpath(strict=True)``，它内部以 ``FILE_FLAG_BACKUP_SEMANTICS`` 打开并取 ``GetFinalPathNameByHandleW``，
    目录同样适用（``os.open`` 在 Windows 上打不开目录）；其他平台 ``realpath(strict=True)``。

    异常:
        OSError: 对象不存在或无法打开（macOS 上目录需要读权限）。
    """

    if sys.platform != "darwin":
        return Path(os.path.realpath(path, strict=True))
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return canonical_path_of(descriptor)
    finally:
        os.close(descriptor)


def canonical_path_of(descriptor: int) -> Path:
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
    if sys.platform == "darwin":
        import fcntl

        # Darwin F_GETPATH 返回该句柄的绝对路径，内核要求 MAXPATHLEN 缓冲区。
        return Path(os.fsdecode(fcntl.fcntl(descriptor, 50, bytes(1024)).split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{descriptor}"))


__all__ = [
    "FileIdentity",
    "RootDirectory",
    "UnsupportedFileSystemError",
    "canonical_path",
    "canonical_path_of",
]
