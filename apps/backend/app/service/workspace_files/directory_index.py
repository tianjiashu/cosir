"""目录分页的临时排序索引；超出内存预算后使用系统临时目录，绝不写 workspace。"""

import json
import sqlite3
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from app.service.workspace_files.file_models import FileEntry
from app.service.workspace_files.workspace_paths import natural_key

MEMORY_ENTRIES = 10_000


class DirectoryIndex:
    """小目录内存排序，大目录由 SQLite 外部排序；缓存失效负责释放临时文件。"""

    def __init__(
        self, entries: Iterable[FileEntry], cancelled: threading.Event | None = None
    ) -> None:
        self._cancelled = cancelled
        self._entries: list[FileEntry] = []
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._database: sqlite3.Connection | None = None
        self.total = 0
        try:
            for entry in entries:
                if self._database is None and len(self._entries) >= MEMORY_ENTRIES:
                    self._database = self._spill()
                if self._database is None:
                    self._entries.append(entry)
                else:
                    self._insert(self._database, entry)
                self.total += 1
            if self._database is None:
                self._entries.sort(key=natural_key)
            else:
                self._database.execute(
                    "CREATE INDEX ordered_entries "
                    "ON entries(folder, name COLLATE natural_name, name)"
                )
                self._database.commit()
                # 请求结束后取消标记会置位；后续分页仍应能读取这个已完成的索引。
                self._database.set_progress_handler(None, 0)
            self._cancelled = None
        except BaseException:
            self.close()
            raise

    def _spill(self) -> sqlite3.Connection:
        self._temporary = tempfile.TemporaryDirectory(prefix="cosir-directory-")
        self._database = sqlite3.connect(
            Path(self._temporary.name) / "entries.sqlite", check_same_thread=False
        )
        if self._cancelled is not None:
            cancelled = self._cancelled
            self._database.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
        self._database.execute("PRAGMA cache_size=-2048")
        self._database.execute("PRAGMA temp_store=FILE")
        self._database.create_collation("natural_name", self._compare)
        self._database.execute("CREATE TABLE entries(folder INTEGER, name TEXT, payload TEXT)")
        for entry in self._entries:
            self._insert(self._database, entry)
        self._entries.clear()
        return self._database

    @staticmethod
    def _compare(left: str, right: str) -> int:
        a = natural_key(FileEntry("", left, "file"))
        b = natural_key(FileEntry("", right, "file"))
        return (a > b) - (a < b)

    @staticmethod
    def _insert(database: sqlite3.Connection, entry: FileEntry) -> None:
        database.execute(
            "INSERT INTO entries VALUES (?, ?, ?)",
            (entry.kind != "directory", entry.name, json.dumps(asdict(entry), ensure_ascii=False)),
        )

    def page(self, offset: int, limit: int) -> list[FileEntry]:
        if self._database is None:
            return self._entries[offset : offset + limit]
        rows = self._database.execute(
            "SELECT payload FROM entries "
            "ORDER BY folder, name COLLATE natural_name, name LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [FileEntry(**json.loads(row[0])) for row in rows]

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._entries.clear()
