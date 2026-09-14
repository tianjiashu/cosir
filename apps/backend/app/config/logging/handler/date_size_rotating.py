"""按本地日期和文件大小轮转 JSONL 日志。"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path


def dated_log_path(base_filename: str | Path, log_date: date | str) -> Path:
    """根据逻辑日志文件名生成日期分片路径。

    参数:
        base_filename: 逻辑日志文件名，例如 ``backend.log``。
        log_date: 本地日期或 ``YYYY-MM-DD`` 日期文本。

    返回:
        带日期的日志路径，例如 ``backend-2026-09-13.log``。

    异常:
        ValueError: 逻辑文件名没有有效的文件名部分时抛出。

    副作用:
        无；不会创建目录或文件。
    """

    base_path = Path(base_filename)
    stem = base_path.stem
    if not stem:
        raise ValueError("日志文件名不能为空")
    suffix = base_path.suffix
    date_text = log_date.isoformat() if isinstance(log_date, date) else str(log_date)
    filename = f"{stem}-{date_text}{suffix}"
    return base_path.with_name(filename)


class DateSizeRotatingFileHandler(RotatingFileHandler):
    """同时按本地日期和单文件大小轮转的文件 handler。

    ``baseFilename`` 是逻辑文件名，实际写入文件为带日期的文件；同一天内
    达到 ``maxBytes`` 后使用 ``.1``、``.2`` 等后缀保留历史分片。该 handler
    仅负责文件日志，不负责 SQLite 日志副本或日志内容脱敏。

    参数:
        filename: 逻辑日志文件路径。
        maxBytes: 单个日期分片的最大字节数；0 表示不启用大小轮转。
        backupCount: 同一日期最多保留的历史大小分片数量。

    副作用:
        创建日期日志文件并在写入前执行重命名轮转。轮转失败沿用 logging
        handler 的 ``handleError`` 降级行为，不改变调用方业务异常契约。
    """

    def __init__(
        self,
        filename: str | os.PathLike[str],
        *,
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str = "utf-8",
    ) -> None:
        self._logical_filename = Path(filename)
        self._active_date = date.today().isoformat()
        initial_filename = dated_log_path(self._logical_filename, self._active_date)
        super().__init__(
            initial_filename,
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=True,
        )

    def shouldRollover(self, record) -> bool:
        """在 logging 写入当前记录前检查日期和字节预算。"""

        self._switch_to_current_date()
        if self.maxBytes <= 0:
            return False

        current_size = self._current_size()
        encoded_record = (self.format(record) + self.terminator).encode(
            self.encoding or "utf-8",
            errors="replace",
        )
        return current_size > 0 and current_size + len(encoded_record) > self.maxBytes

    def doRollover(self) -> None:
        """关闭当前流并将同一日期的历史分片向后移动。"""

        self._close_stream()
        if self.backupCount <= 0:
            return

        active_path = Path(self.baseFilename)
        for index in range(self.backupCount - 1, 0, -1):
            source = self._shard_path(active_path, index)
            target = self._shard_path(active_path, index + 1)
            if source.exists():
                self._remove_if_exists(target)
                os.replace(source, target)

        if active_path.exists():
            first = self._shard_path(active_path, 1)
            self._remove_if_exists(first)
            os.replace(active_path, first)

    def _switch_to_current_date(self) -> None:
        current_date = date.today().isoformat()
        if current_date == self._active_date:
            return
        self._close_stream()
        self.baseFilename = os.path.abspath(
            os.fspath(dated_log_path(self._logical_filename, current_date))
        )
        self._active_date = current_date

    def _current_size(self) -> int:
        try:
            return os.path.getsize(self.baseFilename)
        except FileNotFoundError:
            return 0

    def _close_stream(self) -> None:
        if self.stream is None:
            return
        self.stream.flush()
        self.stream.close()
        self.stream = None  # type: ignore[assignment]

    @staticmethod
    def _shard_path(active_path: Path, index: int) -> Path:
        return active_path.with_name(
            f"{active_path.stem}.{index}{active_path.suffix}"
        )

    @staticmethod
    def _remove_if_exists(path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()
