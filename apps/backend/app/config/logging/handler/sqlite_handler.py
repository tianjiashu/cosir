"""异步 SQLite 日志 handler。"""

import logging
import queue
import sys
import threading
import time
from collections import deque

from app.models.mapped_log_record import LogError, MappedLogRecord
from app.storage.crud.log_crud import LogStore
from app.models import LogEntryRecord

HIGH_PRIORITY_LEVELS = {"WARNING", "ERROR", "CRITICAL"}


class SQLiteLogHandler(logging.Handler):
    """将日志记录异步写入独立 SQLite 日志库。"""

    def __init__(
        self,
        store: LogStore,
        queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval_seconds: float = 1.0,
        fallback_handler: logging.Handler | None = None,
    ) -> None:
        """初始化 SQLite 日志 handler。

        参数:
            store: 日志 SQLite 存储。
            queue_size: 内存队列最大长度。
            batch_size: 单次批量写入最大条数。
            flush_interval_seconds: 后台线程最大等待刷盘秒数。
            fallback_handler: 可选文件 handler，用于限频写入日志系统自身告警。

        返回:
            无。

        异常:
            ValueError: 如果队列、批量或 flush 间隔非法。

        副作用:
            启动后台写入线程。
        """

        super().__init__()
        if queue_size < 1:
            raise ValueError("queue_size must be greater than zero")
        if batch_size < 1:
            raise ValueError("batch_size must be greater than zero")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be greater than zero")
        self._store = store
        self._queue: queue.Queue[LogEntryRecord] = queue.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._fallback_handler = fallback_handler
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sqlite-log-writer", daemon=True)
        self._last_warning_at = 0.0
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志记录非阻塞放入 SQLite 写入队列。

        参数:
            record: Python logging 传入的记录。

        返回:
            无。

        异常:
            无。所有异常都会降级处理，避免影响业务流程。

        副作用:
            可能向内存队列追加日志记录；队列满时可能丢弃 SQLite 入库副本。
        """

        if getattr(record, "_skip_sqlite_log", False):
            return
        try:
            entry = entry_from_log_record(record)
            self._put_nonblocking(entry)
        except Exception as exc:
            self._warn_once("sqlite_log_enqueue_failed", exc)

    def close(self) -> None:
        """停止后台线程并尽量 flush 队列。

        参数:
            无。

        返回:
            无。

        异常:
            无。关闭失败不向调用方抛出。

        副作用:
            等待后台线程短暂退出并关闭 handler。
        """
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._flush_interval_seconds * 2))
        super().close()

    def flush(self) -> None:
        """等待队列中当前日志被后台线程写入。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            最多等待短暂时间让后台线程刷盘。
        """
        deadline = time.monotonic() + max(1.0, self._flush_interval_seconds * 3)
        while self._queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    def _put_nonblocking(self, entry: LogEntryRecord) -> None:
        """非阻塞写入队列，必要时丢弃低优先级日志。

        参数:
            entry: 待入队日志记录。

        返回:
            无。

        异常:
            无。

        副作用:
            修改内存队列；可能丢弃日志入库副本。
        """
        try:
            self._queue.put_nowait(entry)
            return
        except queue.Full:
            pass
        if entry.level in HIGH_PRIORITY_LEVELS and self._drop_low_priority():
            try:
                self._queue.put_nowait(entry)
                self._warn_once(
                    "sqlite_log_queue_overflow", RuntimeError("dropped low priority log entry")
                )
                return
            except queue.Full:
                pass
        self._warn_once("sqlite_log_queue_overflow", RuntimeError("dropped sqlite log entry"))

    def _drop_low_priority(self) -> bool:
        """丢弃一个低优先级队列项为高等级日志腾出空间。

        参数:
            无。

        返回:
            成功丢弃低优先级日志时为 True。

        异常:
            无。

        副作用:
            重排内存队列。
        """
        drained: deque[LogEntryRecord] = deque()
        dropped = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if not dropped and item.level not in HIGH_PRIORITY_LEVELS:
                dropped = True
                self._queue.task_done()
                continue
            drained.append(item)
        while drained:
            item = drained.popleft()
            self._queue.task_done()
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break
        return dropped

    def _run(self) -> None:
        """后台消费队列并批量写入 SQLite。

        参数:
            无。

        返回:
            无。

        异常:
            无。写入失败会降级到文件或 stderr 告警。

        副作用:
            持续消费队列并写入日志数据库。
        """
        batch: list[LogEntryRecord] = []
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                batch.append(self._queue.get(timeout=self._flush_interval_seconds))
            except queue.Empty:
                if batch:
                    self._write_batch(batch)
                    batch = []
                continue
            if len(batch) >= self._batch_size or self._stop_event.is_set():
                self._write_batch(batch)
                batch = []
        if batch:
            self._write_batch(batch)

    def _write_batch(self, batch: list[LogEntryRecord]) -> None:
        """写入一批日志记录。

        参数:
            batch: 待写入日志记录列表。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 SQLite；失败时写限频告警。
        """
        try:
            self._store.insert_many(batch)
        except Exception as exc:
            self._warn_once("sqlite_log_write_failed", exc)
        finally:
            for _entry in batch:
                self._queue.task_done()

    def _warn_once(self, event_name: str, error: Exception) -> None:
        """限频记录日志系统自身告警。

        参数:
            event_name: 稳定事件名。
            error: 触发告警的异常。

        返回:
            无。

        异常:
            无。

        副作用:
            可能向文件 handler 或 stderr 写入告警。
        """
        now = time.monotonic()
        if now - self._last_warning_at < 5:
            return
        self._last_warning_at = now
        message = f"{event_name}: {error}"
        if self._fallback_handler is None:
            print(message, file=sys.stderr)
            return
        record = logging.LogRecord(
            name="coding_agent.backend",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg=event_name,
            args=(),
            exc_info=None,
        )
        record._skip_sqlite_log = True
        record.display_message = message
        try:
            self._fallback_handler.handle(record)
        except Exception:
            print(message, file=sys.stderr)


def entry_from_log_record(record: logging.LogRecord) -> LogEntryRecord:
    """把 LogRecord 转换为 SQLite 日志记录。

    参数:
        record: Python logging 记录。

    返回:
        可写入 SQLite 的 LogEntryRecord。

    异常:
        TypeError: 如果 data 无法脱敏或 JSON 化。

    副作用:
        无。
    """
    mapped = MappedLogRecord.from_record(record)
    data = mapped.data
    error: LogError | None = mapped.error
    return LogEntryRecord(
        ts=mapped.ts,
        level=mapped.level,
        logger=mapped.logger,
        trace_id=mapped.trace_id,
        caller=mapped.caller,
        event=mapped.event,
        msg=mapped.msg,
        data=data,
        error=error.__dict__ if error is not None else None,
        truncated=mapped.truncated,
    )
