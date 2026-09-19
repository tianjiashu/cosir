"""为受保护的文件变更捕获不跟随最终符号链接的文件系统状态。

本模块中的函数只读取工作区路径，不执行恢复、数据库持久化或其他写入；ChangeSet 服务和文件变更守卫共用这些函数。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UnsupportedWorkspaceEntry(RuntimeError):
    """当某个条目无法在不跟随链接的情况下捕获和恢复时抛出。"""


@dataclass(frozen=True)
class CapturedPath:
    """保存工作区相对路径、序列化状态以及可选的原始文件内容。

    ``state`` 包含快照协调所需的路径身份字段。只有普通文件会填充 ``content``，供调用方暂存变更前的文件内容。
    """

    path: str
    state: dict[str, Any]
    content: bytes | None = None


def capture_path(root: Path, path: Path, *, read_content: bool = True) -> CapturedPath:
    """捕获一个工作区路径的状态，不跟随路径末尾的符号链接。

    参数:
        root: 已解析的工作区边界路径。
        path: 要检查的绝对路径或相对于工作区根目录的路径。
        read_content: 是否读取并返回普通文件的完整字节。``False`` 时只做流式
            ``sha256`` 哈希（不把内容载入内存），用于删除等无需正文即可回退的场景，
            避免大文件/二进制文件整篇读入内存。

    返回:
        相对路径、序列化的身份状态，以及存在时的普通文件字节内容（``read_content``
        为 ``False`` 时为 ``None``）。

    异常:
        OSError: 路径越出工作区，或无法读取。
        UnsupportedWorkspaceEntry: 路径是重解析点或不受支持的特殊条目。

    副作用:
        读取路径元数据；``read_content`` 为 ``True`` 时还会读取普通文件内容，否则只
        做分块哈希；不会修改工作区。
    """
    root_path = root.resolve()
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise OSError(errno.EPERM, "ChangeSet path escapes the workspace") from exc

    if not os.path.lexists(absolute):
        return CapturedPath(relative, {"exists": False})

    try:
        absolute.parent.resolve(strict=True).relative_to(root_path)
    except (OSError, ValueError) as exc:
        raise OSError(errno.EPERM, "ChangeSet entry parent escapes the workspace") from exc

    metadata = absolute.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(absolute)
        target_is_directory = _symlink_target_is_directory(absolute)
        encoded = json.dumps(
            {"entry_type": "symlink", "target": target, "target_is_directory": target_is_directory},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return CapturedPath(
            relative,
            {
                "exists": True,
                "entry_type": "symlink",
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "target": target,
                "target_is_directory": target_is_directory,
            },
        )

    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        raise UnsupportedWorkspaceEntry("unsupported reparse point in workspace")

    if stat.S_ISDIR(metadata.st_mode):
        return CapturedPath(relative, {"exists": True, "entry_type": "directory"})
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsupportedWorkspaceEntry("unsupported non-regular workspace entry")

    if read_content:
        content = absolute.read_bytes()
        return CapturedPath(
            relative,
            {
                "exists": True,
                "entry_type": "file",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            content,
        )
    # 只做流式哈希，不把文件正文载入内存，供删除等回退事实无需正文的场景使用。
    return CapturedPath(
        relative,
        {
            "exists": True,
            "entry_type": "file",
            "sha256": _streaming_sha256(absolute),
        },
        None,
    )


def state_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """比较路径身份字段，并忽略恢复引用。

    参数:
        left: 第一个序列化路径状态。
        right: 第二个序列化路径状态。

    返回:
        两个状态的存在性、类型、摘要和符号链接身份是否相同。

    异常:
        字典输入不会引发预期异常。

    副作用:
        无。
    """
    fields = ("exists", "entry_type", "sha256", "target", "target_is_directory")
    return all(left.get(key) == right.get(key) for key in fields)


def directory_identity(path: Path) -> str | None:
    """返回目录身份，供清理前进行不跟随符号链接的校验。

    参数:
        path: 要检查的目录路径。

    返回:
        设备号与 inode 组成的身份标识；若路径不是可访问的目录则返回 ``None``。

    异常:
        不抛出文件系统异常；元数据读取错误以 ``None`` 表示。

    副作用:
        读取文件系统元数据，不跟随路径末尾的组件。
    """
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or not metadata.st_ino:
        return None
    return f"{metadata.st_dev:x}:{metadata.st_ino:x}"


def is_relative_ancestor(parent: str, child: str) -> bool:
    """检查一个规范化的工作区相对路径是否是另一个路径的严格祖先。

    参数:
        parent: 候选祖先路径，不带末尾斜杠。
        child: 候选后代路径。

    返回:
        仅当 ``child`` 以 ``parent`` 加路径分隔符开头时返回 ``True``。

    异常:
        不会引发预期异常。

    副作用:
        无。
    """
    return bool(parent) and child.startswith(f"{parent}/")


def _symlink_target_is_directory(path: Path) -> bool:
    """仅读取链接目标的元数据，以保留创建链接时所需的链接类型。"""
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except OSError:
        return False


def _streaming_sha256(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """对普通文件做分块 ``sha256`` 哈希，不把内容载入内存。

    参数:
        path: 待哈希的普通文件绝对路径。
        chunk_size: 单次读取的字节数（默认 1 MiB）。

    返回:
        『小写十六进制」的 ``sha256`` 摘要。

    异常:
        OSError: 文件无法读取。
        IsADirectoryError: 路径是目录而非普通文件。

    副作用:
        读取文件内容（分块），不修改文件。
    """

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CapturedPath",
    "UnsupportedWorkspaceEntry",
    "capture_path",
    "directory_identity",
    "is_relative_ancestor",
    "state_matches",
]
