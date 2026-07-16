"""SQLite 日志存储。"""

import json
import logging
import sys
from pathlib import Path
import sqlite3
from typing import Any

from app.storage.log_records import LogEntryRecord, LogQuery


SCHEMA_VERSION = 2


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

        旧版本（v1）按决策不迁移，直接重建表（D3）。

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
                    ts, level, logger, trace_id, caller, event, msg,
                    data_json, error_json, truncated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """创建第二版日志 schema（9 字段，仅 trace_id 链路键）。

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
          logger TEXT NOT NULL,
          trace_id TEXT,
          caller TEXT,
          event TEXT NOT NULL,
          msg TEXT NOT NULL,
          data_json TEXT NOT NULL DEFAULT '{}',
          error_json TEXT,
          truncated INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_log_entries_trace_ts ON log_entries(trace_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_level_ts ON log_entries(level, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_event_ts ON log_entries(event, ts)",
        "CREATE INDEX IF NOT EXISTS idx_log_entries_ts ON log_entries(ts)",
    ):
        connection.execute(sql)


def _migrate_schema(connection: sqlite3.Connection, current_version: int, target_version: int) -> None:
    """执行日志 schema 迁移。

    按决策 D3 不保留历史数据：旧版本直接重建表。

    参数:
        connection: 已打开的 SQLite 连接。
        current_version: 当前 schema 版本。
        target_version: 目标 schema 版本。

    返回:
        无。

    异常:
        sqlite3.Error: 如果迁移失败。

    副作用:
        删除并重建日志表，更新 schema 版本。
    """
    connection.execute("DROP TABLE IF EXISTS log_entries")
    _create_schema(connection)
    connection.execute(f"PRAGMA user_version = {target_version}")
    # D3：旧库不迁移，重建为不可逆操作，写一条 info 便于运维确认历史日志已丢弃。
    # 此时 SQLite handler 尚未挂到 logger（file handler 已挂），日志只落 JSONL，不会递归写库。
    try:
        logging.getLogger("coding_agent.backend").info(
            "log_schema_rebuilt",
            extra={
                "msg": "日志 SQLite schema 已重建（旧库不迁移，历史日志已丢弃）",
                "data": {"old_version": current_version, "new_version": target_version},
            },
        )
    except Exception as exc:  # noqa: BLE001
        # 重建日志失败不影响 schema 升级本身；此时 SQLite handler 未挂载，
        # 用 stderr 兜底输出上下文，避免静默吞异常导致无法排查。
        sys.stderr.write(
            f"[log_store] 写 schema 重建事件日志失败（旧库已丢弃）："
            f"old_version={current_version}, new_version={target_version}, error={exc!r}\n"
        )


def _entry_values(entry: LogEntryRecord) -> tuple[Any, ...]:
    """把日志记录转换为 INSERT 参数。

    参数:
        entry: 日志记录。

    返回:
        与 INSERT 语句列顺序一致的参数元组。

    异常:
        TypeError: 如果 data 或 error 无法 JSON 序列化。

    副作用:
        无。
    """
    return (
        entry.ts,
        entry.level,
        entry.logger,
        entry.trace_id,
        entry.caller,
        entry.event,
        entry.msg,
        json.dumps(entry.data, ensure_ascii=False),
        json.dumps(entry.error, ensure_ascii=False) if entry.error else None,
        1 if entry.truncated else 0,
    )


def _entry_from_row(row: sqlite3.Row) -> LogEntryRecord:
    """把 SQLite 行转换为日志记录。

    参数:
        row: SQLite 查询结果行。

    返回:
        LogEntryRecord 实例。

    异常:
        json.JSONDecodeError: 如果 data_json 或 error_json 不是合法 JSON。

    副作用:
        无。
    """
    data = json.loads(row["data_json"] or "{}")
    error = json.loads(row["error_json"]) if row["error_json"] else None
    return LogEntryRecord(
        ts=row["ts"],
        level=row["level"],
        logger=row["logger"],
        trace_id=row["trace_id"] or "",
        caller=row["caller"] or "",
        event=row["event"],
        msg=row["msg"],
        data=data if isinstance(data, dict) else {},
        error=error if isinstance(error, dict) else None,
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
        ("event", query.event_name),
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
