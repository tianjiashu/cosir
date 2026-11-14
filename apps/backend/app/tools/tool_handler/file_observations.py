"""文件观察记录：每个 Session 读过或写过哪些文件、当时文件是什么状态。

只守 ``write_file`` 整文件覆盖：覆盖已有文件前，本 Session 必须看过它（``read_file`` 读过，或自己刚用
文件工具写过），且之后没被别人改过（用户、命令、其他 Session）。``edit_file`` 与 ``apply_patch`` 不查：
它们要求原文完全一致，原文对不上就失败，本身已防住覆盖没看过的内容。

记的是文件的 ``(mtime_ns, size)``：读或写之后现取，比对时再取一次。进程内、不持久化：后端重启或分叉
出的新 Session 都要重读一次才能覆盖（Claude Code 同样按对话记）。Session 删除提交后由
``SessionService`` 回收。工具在线程池里并发执行，读写都在锁内。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


class FileObservations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[int, str], tuple[int, int]] = {}

    @staticmethod
    def _key(session_id: int, path: Path) -> tuple[int, str]:
        return session_id, os.path.realpath(path)

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def record(self, session_id: int, path: Path) -> None:
        """在读完或写完之后调用；取不到状态（文件已不在）时忘掉这条。"""

        key, stamp = self._key(session_id, path), self._stamp(path)
        with self._lock:
            if stamp is None:
                self._seen.pop(key, None)
            else:
                self._seen[key] = stamp

    def overwrite_refusal(self, session_id: int, path: Path) -> str | None:
        """覆盖已有文件 ``path`` 前调用；可以覆盖返回 ``None``，否则返回面向模型的拒绝原因。"""

        key, stamp = self._key(session_id, path), self._stamp(path)
        with self._lock:
            seen = self._seen.get(key)
        if seen is None:
            return (
                "this file already exists and you have not read it in this conversation. Read it "
                "with read_file first, or use edit_file for a partial change"
            )
        if seen != stamp:
            return (
                "this file changed after you last read it (by the user, a command or another "
                "session). Read it again with read_file before overwriting it"
            )
        return None

    def drop_session(self, session_id: int) -> None:
        with self._lock:
            for key in [key for key in self._seen if key[0] == session_id]:
                del self._seen[key]


file_observations = FileObservations()
