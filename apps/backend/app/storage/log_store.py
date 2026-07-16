"""SQLite 日志存储。"""

import json
from pathlib import Path
import sqlite3
from typing import Any

from app.storage.log_records import LogEntryRecord, LogQuery


SCHEMA_VERSION = 1


class LogStore:
    """读写独立日志 SQLite 数据库。"""

    def __init__(self, database_path: Path) -> None:
        """初始化日志数据库并确保 schema 存在。

        参数:
            database_path: 独立日志 SQLite 数据库路径。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlite3.Error: 如果初始化 schema 失败。

        副作用:
            创建数据库目录与日志表。
        """

        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        """初始化日志表、索引和 schema 版本。

        参数:
            无。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 PRAGMA 或 DDL 执行失败。

        副作用:
            修改日志数据库 schema。
        """

        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version == 0:
                _create_schema(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current_version < SCHEMA_VERSION:
                _migrate_schema(connection, current_version, SCHEMA_VERSION)

    def insert_many(self, entries: list[LogEntryRecord]) -> None:
        """批量写入日志记录。

        参数:
            entries: 待写入的日志记录列表。

        返回:
            无。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            向日志 SQLite 写入多条记录。
        """

        if not entries:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO log_entries(
                    ts, level, logger_name, event_name, message, trace_id, task_id,
                    run_id, span_id, event_id, step_id, tool_call_id, approval_id,
                    error_type, error_message, attributes_json, stack, truncated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_entry_values(entry) for entry in entries],
            )

    def query(self, query: LogQuery) -> list[LogEntryRecord]:
        """按条件查询日志记录。

        参数:
            query: 查询参数。

        返回:
            匹配的日志记录列表。

        异常:
            ValueError: 如果 limit 或排序方向非法。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        _validate_query(query)
        where, values = _build_where(query)
        order = "ASC" if query.order == "asc" else "DESC"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM log_entries
                {where}
                ORDER BY ts {order}, id {order}
                LIMIT ?
                """,
                (*values, query.limit),
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        """打开日志 SQLite 连接并配置本地可靠性 PRAGMA。

        参数:
            无。

        返回:
            已配置 row_factory 的 SQLite 连接。

        异常:
            sqlite3.Error: 如果连接或 PRAGMA 执行失败。

        副作用:
            打开数据库连接，并可能创建 WAL 文件。
        """

        connection = sqlite3.connect(self._database_path, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=3000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    """创建第一版日志 schema。

    参数:
        connection: 已打开的 SQLite 连接。

    返回:
        无。

    异常:
        sqlite3.Error: 如果 DDL 执行失败。

    副作用:
        创建表和索引。
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS log_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          level TEXT NOT NULL,
          logger_name TEXT NOT NULL,
          event_name TEXT NOT NULL,
          message TEXT NOT NULL,
          trace_id TEXT,
          task_id TEXT,
          run_id TEXT,
          span_id TEXT,
          event_id TEXT,
          step_id TEXT,
          tool_call_id TEXT,
          approval_id TEXT,
          error_type TEXT,
          error_message TEXT,
          attributes_json TEXT NOT NULL,
          stack TEXT,
          truncated INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_log_entries_trace_ts ON log_entries(trace_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_task_ts ON log_entries(task_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_run_ts ON log_entries(run_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_level_ts ON log_entries(level, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_ts ON log_entries(ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_event_ts ON log_entries(event_name, ts)",
    ):
        connection.execute(sql)


def _migrate_schema(connection: sqlite3.Connection, current_version: int, target_version: int) -> None:
    """执行日志 schema 增量迁移。

    参数:
        connection: 已打开的 SQLite 连接。
        current_version: 当前 schema 版本。
        target_version: 目标 schema 版本。

    返回:
        无。

    异常:
        sqlite3.Error: 如果迁移失败。

    副作用:
        修改数据库 schema 版本。
    """

    if current_version < target_version:
        connection.execute(f"PRAGMA user_version = {target_version}")


def _entry_values(entry: LogEntryRecord) -> tuple[Any, ...]:
    """把日志记录转换为 INSERT 参数。

    参数:
        entry: 日志记录。

    返回:
        与 INSERT 语句列顺序一致的参数元组。

    异常:
        TypeError: 如果 attributes 无法 JSON 序列化。

    副作用:
        无。
    """

    return (
        entry.ts,
        entry.level,
        entry.logger_name,
        entry.event_name,
        entry.message,
        entry.trace_id,
        entry.task_id,
        entry.run_id,
        entry.span_id,
        entry.event_id,
        entry.step_id,
        entry.tool_call_id,
        entry.approval_id,
        entry.error_type,
        entry.error_message,
        json.dumps(entry.attributes, ensure_ascii=False),
        entry.stack,
        1 if entry.truncated else 0,
    )


def _entry_from_row(row: sqlite3.Row) -> LogEntryRecord:
    """把 SQLite 行转换为日志记录。

    参数:
        row: SQLite 查询结果行。

    返回:
        LogEntryRecord 实例。

    异常:
        json.JSONDecodeError: 如果 attributes_json 不是合法 JSON。

    副作用:
        无。
    """

    attributes = json.loads(row["attributes_json"] or "{}")
    return LogEntryRecord(
        ts=row["ts"],
        level=row["level"],
        logger_name=row["logger_name"],
        event_name=row["event_name"],
        message=row["message"],
        trace_id=row["trace_id"] or "",
        task_id=row["task_id"] or "",
        run_id=row["run_id"] or "",
        span_id=row["span_id"] or "",
        event_id=row["event_id"] or "",
        step_id=row["step_id"] or "",
        tool_call_id=row["tool_call_id"] or "",
        approval_id=row["approval_id"] or "",
        error_type=row["error_type"] or "",
        error_message=row["error_message"] or "",
        attributes=attributes if isinstance(attributes, dict) else {},
        stack=row["stack"] or "",
        truncated=bool(row["truncated"]),
    )


def _validate_query(query: LogQuery) -> None:
    """校验查询参数。

    参数:
        query: 查询参数。

    返回:
        无。

    异常:
        ValueError: 如果 limit 或排序方向非法。

    副作用:
        无。
    """

    if query.limit < 1:
        raise ValueError("limit must be greater than zero")
    if query.order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")


def _build_where(query: LogQuery) -> tuple[str, list[Any]]:
    """根据查询参数构造 SQL WHERE 片段。

    参数:
        query: 查询参数。

    返回:
        WHERE SQL 片段和参数列表。

    异常:
        无。

    副作用:
        无。
    """

    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("trace_id", query.trace_id),
        ("level", query.level),
        ("event_name", query.event_name),
        ("task_id", query.task_id),
        ("run_id", query.run_id),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    if query.start_time:
        clauses.append("ts >= ?")
        values.append(query.start_time)
    if query.end_time:
        clauses.append("ts <= ?")
        values.append(query.end_time)
    if not clauses:
        return "", values
    return "WHERE " + " AND ".join(clauses), values
