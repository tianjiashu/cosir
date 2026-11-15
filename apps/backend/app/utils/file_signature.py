"""文件签名：一组路径的修改时间与大小，用来判断进程内的配置副本要不要重读。

配置文件是唯一事实源，进程内的副本（提示词来源的投影缓存、Agent 注册表）读取前先比签名：没变就复用，
变了当场重读。手动编辑、外部工具改文件与配置页写入因此走同一条路，不需要文件监听或写入方通知。

只认修改时间（纳秒）与大小：同一时刻写入等长内容会被当成没变，可接受。stat 一个路径约几微秒，
远低于重读与解析。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

FileSignature = tuple[tuple[str, int | None, int | None], ...]


def stat_signature(paths: Iterable[Path]) -> FileSignature:
    """每个路径的修改时间与大小，不存在或不可读记为 ``None``；跟随符号链接（链接目标变了也算变化）。"""

    signature: list[tuple[str, int | None, int | None]] = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            signature.append((str(path), None, None))
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def directory_entries(directory: Path, suffix: str = "") -> list[Path]:
    """目录本身加上其下一层名称以 ``suffix`` 结尾、不以点开头的条目，按名称排序；目录读不了时只有它本身。

    增删条目会改变目录自己的修改时间，条目内容变了会改变条目的修改时间，两者合起来覆盖一层目录的变化。
    """

    try:
        names = sorted(
            entry.name
            for entry in os.scandir(directory)
            if entry.name.endswith(suffix) and not entry.name.startswith(".")
        )
    except OSError:
        return [directory]
    return [directory, *(directory / name for name in names)]
