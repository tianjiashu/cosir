"""打开句柄是内容读取的唯一范围边界，避免软链接在检查和打开之间被替换。"""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.utils.path.file_identity import RootDirectory, canonical_path_of


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
        if not RootDirectory.at(root).contains(canonical_path_of(descriptor)):
            raise PermissionError("链接目标位于工作区之外")
        yield source
