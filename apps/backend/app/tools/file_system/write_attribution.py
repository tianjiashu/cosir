"""记下哪个会话把文件写成了哪个版本：读后变更通知据此写明修改者。

进程内诊断旁路，不是业务事实：有上限、重启即空，查不到就说「修改者不明」，不影响写入检查。
两种来源：文件工具确认的写入（写成后自己登记，确定）；终端命令结束时核对到的、本会话读过的文件
的变化（命令期间可能混着别的进程的写入，标为 ``by_command``）。存在的版本按 ``FileVersion`` 登记
（版本含 inode 与时间戳，同一键就是同一次写入的结果）；删除没有版本，按路径登记。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from app.tools.file_system.facts.file_facts import FileFacts
from app.tools.file_system.facts.file_version import FileVersion


@dataclass(frozen=True)
class FileWriter:
    session_id: int
    # 终端命令期间观察到的变化，不是工具确认的写入。
    by_command: bool


class WriteAttribution:
    """任意线程可调：文件工具在工作线程登记，终端命令在作业线程登记，通知在 prepare_input 读取。"""

    _LIMIT = 4096

    def __init__(self) -> None:
        self._lock = Lock()
        self._writers: OrderedDict[FileVersion | Path, FileWriter] = OrderedDict()

    def record(self, session_id: int, facts: FileFacts) -> None:
        """登记文件工具确认的写入（``FileMutation.after``）。"""
        for mutation in facts.mutations:
            if mutation.after is not None:
                self._put(mutation.after, FileWriter(session_id, by_command=False))

    def record_command(
        self, session_id: int, changed: Iterable[tuple[str, FileVersion | None]]
    ) -> None:
        """登记终端命令结束时核对到的变化（``FileKnowledge.stale`` 的结果）。"""
        writer = FileWriter(session_id, by_command=True)
        for path, version in changed:
            self._put(Path(path) if version is None else version, writer)

    def writer_of(self, path: Path, version: FileVersion | None) -> FileWriter | None:
        with self._lock:
            return self._writers.get(path if version is None else version)

    def _put(self, key: FileVersion | Path, writer: FileWriter) -> None:
        with self._lock:
            self._writers[key] = writer
            self._writers.move_to_end(key)
            while len(self._writers) > self._LIMIT:
                self._writers.popitem(last=False)


write_attribution = WriteAttribution()
