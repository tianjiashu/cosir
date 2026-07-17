"""SQLite 日志 handler 测试。"""

import logging
import queue
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.config.logging import SQLiteLogHandler, entry_from_log_record
from app.storage.log_records import LogEntryRecord, LogQuery
from app.storage.crud.log import LogStore


class SQLiteLogHandlerTests(unittest.TestCase):
    """校验 SQLite 日志 handler 非阻塞落库行为。"""

    def test_entry_from_log_record_maps_event_and_msg(self) -> None:
        """校验 LogRecord mapper 拆分 event 和 msg。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果映射字段不符合预期。

        副作用:
            无。
        """

        record = logging.getLogger("coding_agent.backend").makeRecord(
            name="coding_agent.backend",
            level=logging.INFO,
            fn=__file__,
            lno=1,
            msg="tool_call_finished",
            args=(),
            exc_info=None,
            extra={"msg": "工具执行完成", "trace_id": "trace-1"},
        )
        entry = entry_from_log_record(record)
        self.assertEqual(entry.event, "tool_call_finished")
        self.assertEqual(entry.msg, "工具执行完成")
        self.assertEqual(entry.trace_id, "trace-1")

    def test_handler_writes_info_to_sqlite(self) -> None:
        """校验 logger.info 会异步写入 SQLite。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果日志未入库。

        副作用:
            写入临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            logger, handler = _logger_with_handler(store)
            try:
                logger.info("log_handler_test", extra={"trace_id": "trace-1"})
                handler.flush()
                time.sleep(0.05)
                entries = store.query(LogQuery(trace_id="trace-1", limit=10))
                self.assertEqual([entry.event for entry in entries], ["log_handler_test"])
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_exception_log_persists_error_fields_and_stack(self) -> None:
        """校验异常日志会写入嵌套错误块。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果错误字段缺失。

        副作用:
            写入临时 SQLite 数据库。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            logger, handler = _logger_with_handler(store)
            try:
                try:
                    raise RuntimeError("boom")
                except RuntimeError:
                    logger.exception("tool_call_failed", extra={"trace_id": "trace-error"})
                handler.flush()
                entries = store.query(LogQuery(trace_id="trace-error", limit=10))
                self.assertEqual(entries[0].error["type"], "RuntimeError")
                self.assertEqual(entries[0].error["message"], "boom")
                self.assertIn("Traceback", entries[0].error["stack"])
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_handler_does_not_raise_when_queue_is_full(self) -> None:
        """校验队列满时 handler 不阻塞也不抛给业务。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 emit 抛出异常。

        副作用:
            启动并关闭临时 handler。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            handler = SQLiteLogHandler(store, queue_size=1, batch_size=10, flush_interval_seconds=10)
            logger = logging.getLogger("sqlite-handler-full-test")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.handlers = [handler]
            try:
                for index in range(20):
                    logger.error("queue_full_error", extra={"index": index})
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_high_priority_log_replaces_low_priority_when_queue_is_full(self) -> None:
        """校验高优先级日志会挤掉低优先级队列项。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果高优先级日志没有被保留。

        副作用:
            创建并关闭临时 handler。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            handler = SQLiteLogHandler(store, queue_size=2, batch_size=10, flush_interval_seconds=10)
            handler.close()
            handler._queue = queue.Queue(maxsize=2)
            handler._queue.put_nowait(_entry("low_first", "INFO"))
            handler._queue.put_nowait(_entry("low_second", "INFO"))

            handler._put_nonblocking(_entry("important_error", "ERROR"))

            queued = []
            while not handler._queue.empty():
                queued.append(handler._queue.get_nowait().event)
            self.assertIn("important_error", queued)
            self.assertEqual(len(queued), 2)
            self.assertNotIn("low_first", queued)

    def test_handler_writes_full_batch_at_batch_size(self) -> None:
        """校验达到 batch_size 时按批写入。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果批量写入大小不符合预期。

        副作用:
            启动并关闭临时 handler。
        """

        class CapturingStore:
            """记录批量写入大小的测试存储。"""

            def __init__(self) -> None:
                """初始化批次记录。

                参数:
                    无。

                返回:
                    无。

                异常:
                    无。

                副作用:
                    无。
                """
                self.batch_sizes: list[int] = []

            def insert_many(self, entries):
                """记录批次大小。

                参数:
                    entries: 待写入日志记录。

                返回:
                    无。

                异常:
                    无。

                副作用:
                    保存批量大小。
                """
                self.batch_sizes.append(len(entries))

        store = CapturingStore()
        handler = SQLiteLogHandler(store, queue_size=10, batch_size=3, flush_interval_seconds=10)
        logger = logging.getLogger("sqlite-handler-batch-test")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        try:
            logger.info("batch_one")
            logger.info("batch_two")
            logger.info("batch_three")
            handler.flush()
            self.assertEqual(store.batch_sizes, [3])
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_handler_flushes_low_volume_batch_before_close(self) -> None:
        """校验低流量日志会按 flush 间隔写入而非等待凑满 batch。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 flush 后日志仍未入库。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            handler = SQLiteLogHandler(store, queue_size=10, batch_size=10, flush_interval_seconds=0.05)
            logger = logging.getLogger("sqlite-handler-flush-test")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.handlers = [handler]
            try:
                logger.info("low_volume_log", extra={"trace_id": "trace-flush"})
                handler.flush()
                entries = store.query(LogQuery(trace_id="trace-flush", limit=10))
                self.assertEqual([entry.event for entry in entries], ["low_volume_log"])
            finally:
                logger.removeHandler(handler)
                handler.close()

    def test_handler_write_failure_does_not_raise_to_business(self) -> None:
        """校验写库失败不会抛给业务调用方。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 logger 调用抛出异常。

        副作用:
            启动并关闭临时 handler。
        """

        class FailingStore:
            """始终写入失败的测试存储。"""

            def insert_many(self, entries):
                """模拟 SQLite 写入失败。

                参数:
                    entries: 待写入日志记录。

                返回:
                    无。

                异常:
                    sqlite3.OperationalError: 始终抛出。

                副作用:
                    无。
                """
                raise sqlite3.OperationalError("database is locked")

        handler = SQLiteLogHandler(FailingStore(), queue_size=10, batch_size=1, flush_interval_seconds=0.01)
        logger = logging.getLogger("sqlite-handler-failure-test")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        try:
            logger.info("write_failure_isolated")
            handler.flush()
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_handler_write_failure_emits_fallback_warning(self) -> None:
        """校验写库失败会通过 fallback handler 记录自身告警。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 fallback 未收到告警。

        副作用:
            启动并关闭临时 handler。
        """

        class FailingStore:
            """始终写入失败的测试存储。"""

            def insert_many(self, entries):
                """模拟 SQLite 写入失败。

                参数:
                    entries: 待写入日志记录。

                返回:
                    无。

                异常:
                    sqlite3.OperationalError: 始终抛出。

                副作用:
                    无。
                """
                raise sqlite3.OperationalError("database is locked")

        class CapturingHandler(logging.Handler):
            """记录 fallback 告警的测试 handler。"""

            def __init__(self) -> None:
                """初始化记录列表。

                参数:
                    无。

                返回:
                    无。

                异常:
                    无。

                副作用:
                    无。
                """
                super().__init__()
                self.records: list[logging.LogRecord] = []

            def emit(self, record: logging.LogRecord) -> None:
                """保存 fallback 日志记录。

                参数:
                    record: fallback 日志记录。

                返回:
                    无。

                异常:
                    无。

                副作用:
                    追加到 records。
                """
                self.records.append(record)

        fallback = CapturingHandler()
        handler = SQLiteLogHandler(
            FailingStore(),
            queue_size=10,
            batch_size=1,
            flush_interval_seconds=0.01,
            fallback_handler=fallback,
        )
        logger = logging.getLogger("sqlite-handler-fallback-test")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        try:
            logger.info("write_failure_fallback")
            handler.flush()
            self.assertEqual(fallback.records[0].msg, "sqlite_log_write_failed")
            self.assertTrue(getattr(fallback.records[0], "_skip_sqlite_log"))
        finally:
            logger.removeHandler(handler)
            handler.close()


def _entry(event: str, level: str) -> LogEntryRecord:
    """构造 handler 队列测试日志。

    参数:
        event: 稳定事件名。
        level: 日志级别。

    返回:
        LogEntryRecord 实例。

    异常:
        无。

    副作用:
        无。
    """
    return LogEntryRecord(
        ts="2026-07-15T10:00:00.000Z",
        level=level,
        logger="coding_agent.backend",
        trace_id="",
        caller="",
        event=event,
        msg=event,
    )


def _logger_with_handler(store: LogStore) -> tuple[logging.Logger, SQLiteLogHandler]:
    """构造挂载 SQLite handler 的测试 logger。

    参数:
        store: 日志存储。

    返回:
        logger 和 handler 元组。

    异常:
        无。

    副作用:
        启动 SQLite handler 后台线程。
    """
    handler = SQLiteLogHandler(store, queue_size=100, batch_size=1, flush_interval_seconds=0.05)
    logger = logging.getLogger("sqlite-handler-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers = [handler]
    return logger, handler


if __name__ == "__main__":
    unittest.main()
