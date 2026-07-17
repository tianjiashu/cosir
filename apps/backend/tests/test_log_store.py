"""日志 SQLite 存储测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.storage.log_records import LogEntryRecord, LogQuery
from app.storage.crud.log import LogStore


class LogStoreTests(unittest.TestCase):
    """校验日志 SQLite 存储的 schema、索引和查询行为。"""

    def test_initialize_creates_schema_indexes_and_user_version(self) -> None:
        """校验初始化会创建表、索引和 schema 版本。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 schema 不符合预期。

        副作用:
            创建临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "logs.sqlite3"
            LogStore(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                columns = {
                    row[1]: row[4]
                    for row in connection.execute(
                        "PRAGMA table_info(log_entries)"
                    ).fetchall()
                }
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA index_list(log_entries)"
                    ).fetchall()
                }
                ddl = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'log_entries'"
                ).fetchone()[0]
            self.assertEqual(version, 2)
            self.assertEqual(journal_mode, "wal")
            self.assertIn("AUTOINCREMENT", ddl)
            self.assertEqual(columns["data_json"], "'{}'")
            self.assertEqual(columns["truncated"], "0")
            self.assertIn("idx_log_entries_ts", indexes)
            self.assertIn("idx_log_entries_event_ts", indexes)
            self.assertIn("idx_log_entries_trace_ts", indexes)
            self.assertIn("idx_log_entries_level_ts", indexes)

    def test_insert_and_query_by_trace_returns_ascending_entries(self) -> None:
        """校验 trace 查询按时间升序返回日志。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果查询结果不符合预期。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            store.insert_many(
                [
                    _entry("2026-07-15T10:00:02.000Z", "second", trace_id="trace-1"),
                    _entry("2026-07-15T10:00:01.000Z", "first", trace_id="trace-1"),
                    _entry("2026-07-15T10:00:03.000Z", "other", trace_id="trace-2"),
                ]
            )
            entries = store.query(LogQuery(trace_id="trace-1", order="asc", limit=10))
            self.assertEqual([entry.event for entry in entries], ["first", "second"])

    def test_recent_query_returns_descending_limited_entries(self) -> None:
        """校验最近日志按时间倒序并遵守 limit。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果排序或 limit 不符合预期。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            store.insert_many(
                [
                    _entry("2026-07-15T10:00:01.000Z", "first"),
                    _entry("2026-07-15T10:00:02.000Z", "second"),
                    _entry("2026-07-15T10:00:03.000Z", "third"),
                ]
            )
            entries = store.query(LogQuery(order="desc", limit=2))
            self.assertEqual([entry.event for entry in entries], ["third", "second"])

    def test_level_and_data_round_trip(self) -> None:
        """校验 level 过滤和 data JSON 读写。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果字段读写不符合预期。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            store.insert_many(
                [
                    _entry("2026-07-15T10:00:01.000Z", "ok", level="INFO"),
                    _entry(
                        "2026-07-15T10:00:02.000Z",
                        "failed",
                        level="ERROR",
                        data={"tool_name": "read_file"},
                    ),
                ]
            )
            entries = store.query(LogQuery(level="ERROR", limit=10))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].data["tool_name"], "read_file")

    def test_time_range_and_event_query_and_invalid_params(self) -> None:
        """校验时间范围、event 查询和非法 limit/order。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果查询过滤不符合预期。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LogStore(Path(temp_dir) / "logs.sqlite3")
            store.insert_many(
                [
                    _entry("2026-07-15T10:00:01.000Z", "first", trace_id="trace-1"),
                    _entry(
                        "2026-07-15T10:00:02.000Z",
                        "target_event",
                        trace_id="trace-1",
                    ),
                    _entry("2026-07-15T10:00:03.000Z", "third", trace_id="trace-1"),
                ]
            )
            entries = store.query(
                LogQuery(
                    trace_id="trace-1",
                    start_time="2026-07-15T10:00:02.000Z",
                    end_time="2026-07-15T10:00:02.000Z",
                    event_name="target_event",
                    limit=10,
                )
            )
            self.assertEqual([entry.event for entry in entries], ["target_event"])
            with self.assertRaises(ValueError):
                store.query(LogQuery(limit=0))
            with self.assertRaises(ValueError):
                store.query(LogQuery(order="sideways"))

    def test_non_object_data_json_returns_empty_data(self) -> None:
        """校验非对象 data_json 会降级为空字典。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非对象 data 没有被隔离。

        副作用:
            写入临时 SQLite 数据库。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "logs.sqlite3"
            store = LogStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO log_entries(
                        ts, level, logger, event, msg, data_json, truncated
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-07-15T10:00:00.000Z",
                        "INFO",
                        "coding_agent.backend",
                        "bad_data",
                        "bad_data",
                        "[1, 2, 3]",
                        0,
                    ),
                )
            entries = store.query(LogQuery(event_name="bad_data", limit=10))
            self.assertEqual(entries[0].data, {})


def _entry(
    ts: str,
    event: str,
    level: str = "INFO",
    trace_id: str = "",
    data: dict | None = None,
) -> LogEntryRecord:
    """构造测试日志记录。

    参数:
        ts: UTC RFC3339 时间。
        event: 稳定事件名。
        level: 日志级别。
        trace_id: 可选 trace 标识。
        data: 可选业务字段。

    返回:
        LogEntryRecord 实例。

    异常:
        无。

    副作用:
        无。
    """
    return LogEntryRecord(
        ts=ts,
        level=level,
        logger="coding_agent.backend",
        trace_id=trace_id,
        caller="",
        event=event,
        msg=event,
        data=data or {},
    )


if __name__ == "__main__":
    unittest.main()
