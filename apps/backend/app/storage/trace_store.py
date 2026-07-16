"""Trace Backbone 的 SQLite 存储。"""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from app.core.trace.records import TraceEventRecord, TraceSpanRecord


class TraceStore:
    """读写 trace_events 与 trace_spans。"""

    def __init__(self, database_path: Path) -> None:
        """初始化 Trace 存储并确保 schema 存在。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            创建数据库目录和 trace 表。
        """

        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def next_sequence(self, run_id: str) -> int:
        """返回同一 run 内下一个 trace event 序号。

        参数:
            run_id: Durable Run 标识。

        返回:
            从 1 开始的单调递增序号。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence FROM trace_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["next_sequence"])

    def append_event(self, event: TraceEventRecord) -> TraceEventRecord:
        """追加一个 trace ledger 事件。

        参数:
            event: 已构造并脱敏的 trace event。

        返回:
            带有事务内分配 sequence_no 的 TraceEventRecord。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            向 SQLite 写入 trace_events。
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence FROM trace_events WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            assigned = replace(event, sequence_no=int(row["next_sequence"]))
            connection.execute(
                """
                INSERT INTO trace_events(
                    event_id, trace_id, run_id, task_id, span_id, parent_span_id,
                    sequence_no, event_type, source, level, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trace_id,
                    event.run_id,
                    event.task_id,
                    event.span_id,
                    event.parent_span_id,
                    assigned.sequence_no,
                    event.event_type,
                    event.source,
                    event.level,
                    json.dumps(event.payload, ensure_ascii=False),
                    _to_text(event.created_at),
                ),
            )
            connection.commit()
            return assigned
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_span(self, span: TraceSpanRecord) -> None:
        """写入一个开始状态的 span。

        参数:
            span: span 记录。

        返回:
            无。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            向 SQLite 写入 trace_spans。
        """

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans(
                    span_id, trace_id, run_id, task_id, parent_span_id, name, kind,
                    status, started_at, ended_at, duration_ms, attributes_json, error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _span_values(span),
            )

    def finish_span(
        self,
        span_id: str,
        status: str,
        ended_at: datetime | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """结束一个 span。

        参数:
            span_id: 待结束 span 标识。
            status: 结束状态。
            ended_at: 可选结束时间，省略时使用当前 UTC。
            error: 可选错误摘要。

        返回:
            无。

        异常:
            KeyError: 如果 span 不存在。
            sqlite3.Error: 如果更新失败。

        副作用:
            更新 trace_spans。
        """

        finished_at = ended_at or datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT started_at FROM trace_spans WHERE span_id = ?",
                (span_id,),
            ).fetchone()
            if row is None:
                raise KeyError(span_id)
            started_at = _from_text(row["started_at"])
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            connection.execute(
                """
                UPDATE trace_spans
                SET status = ?, ended_at = ?, duration_ms = ?, error_json = ?
                WHERE span_id = ?
                """,
                (
                    status,
                    _to_text(finished_at),
                    duration_ms,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    span_id,
                ),
            )

    def list_events(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[TraceEventRecord]:
        """查询 trace event。

        参数:
            trace_id: 可选 trace 过滤条件。
            run_id: 可选 run 过滤条件。
            limit: 最大返回数量。

        返回:
            按 sequence_no 和创建时间排序的 trace event 列表。

        异常:
            ValueError: 如果 limit 小于 1。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        where, values = _build_where(trace_id=trace_id, run_id=run_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM trace_events
                {where}
                ORDER BY run_id ASC, sequence_no ASC, created_at ASC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_trace_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回 trace 摘要列表。

        参数:
            limit: 最大返回数量。

        返回:
            按最近事件倒序排列的 trace 摘要字典。

        异常:
            ValueError: 如果 limit 小于 1。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    trace_id,
                    MIN(task_id) AS task_id,
                    MIN(run_id) AS run_id,
                    COUNT(*) AS event_count,
                    MIN(created_at) AS started_at,
                    MAX(created_at) AS updated_at
                FROM trace_events
                GROUP BY trace_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """返回单个 trace 摘要。

        参数:
            trace_id: Trace 标识。

        返回:
            trace 摘要字典。

        异常:
            KeyError: 如果 trace 不存在。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    trace_id,
                    MIN(task_id) AS task_id,
                    MIN(run_id) AS run_id,
                    COUNT(*) AS event_count,
                    MIN(created_at) AS started_at,
                    MAX(created_at) AS updated_at
                FROM trace_events
                WHERE trace_id = ?
                GROUP BY trace_id
                """,
                (trace_id,),
            ).fetchone()
        if row is None:
            raise KeyError(trace_id)
        return dict(row)

    def list_spans(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[TraceSpanRecord]:
        """查询 trace span。

        参数:
            trace_id: 可选 trace 过滤条件。
            run_id: 可选 run 过滤条件。
            limit: 最大返回数量。

        返回:
            按开始时间排序的 span 列表。

        异常:
            ValueError: 如果 limit 小于 1。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        where, values = _build_where(trace_id=trace_id, run_id=run_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM trace_spans
                {where}
                ORDER BY started_at ASC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [_span_from_row(row) for row in rows]

    def _initialize_schema(self) -> None:
        """创建 trace 表。

        参数:
            无。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            在 SQLite 中创建 trace_events 和 trace_spans。
        """

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    event_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    span_id TEXT,
                    parent_span_id TEXT,
                    sequence_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    level TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trace_events_trace_sequence
                    ON trace_events(trace_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_trace_events_run_sequence
                    ON trace_events(run_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_trace_events_type_created
                    ON trace_events(event_type, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trace_events_run_sequence_unique
                    ON trace_events(run_id, sequence_no);
                CREATE TABLE IF NOT EXISTS trace_spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_ms INTEGER,
                    attributes_json TEXT NOT NULL,
                    error_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trace_spans_trace_started
                    ON trace_spans(trace_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_trace_spans_run_started
                    ON trace_spans(run_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_trace_spans_status_started
                    ON trace_spans(status, started_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """打开 SQLite 连接。

        参数:
            无。

        返回:
            已配置 Row factory 的连接。

        异常:
            sqlite3.Error: 如果数据库无法打开。

        副作用:
            打开数据库连接。
        """

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _build_where(trace_id: str = "", run_id: str = "") -> tuple[str, tuple[str, ...]]:
    """构造 trace 查询条件。

    参数:
        trace_id: trace 过滤条件。
        run_id: run 过滤条件。

    返回:
        SQL WHERE 片段和参数元组。

    异常:
        无。

    副作用:
        无。
    """

    clauses = []
    values = []
    if trace_id:
        clauses.append("trace_id = ?")
        values.append(trace_id)
    if run_id:
        clauses.append("run_id = ?")
        values.append(run_id)
    if not clauses:
        return "", tuple()
    return "WHERE " + " AND ".join(clauses), tuple(values)


def _span_values(span: TraceSpanRecord) -> tuple:
    """将 span 转换为 SQLite 参数。

    参数:
        span: TraceSpanRecord。

    返回:
        INSERT 参数元组。

    异常:
        无。

    副作用:
        无。
    """

    return (
        span.span_id,
        span.trace_id,
        span.run_id,
        span.task_id,
        span.parent_span_id,
        span.name,
        span.kind,
        span.status,
        _to_text(span.started_at),
        _to_text(span.ended_at) if span.ended_at else None,
        span.duration_ms,
        json.dumps(span.attributes, ensure_ascii=False),
        json.dumps(span.error, ensure_ascii=False) if span.error else None,
    )


def _event_from_row(row: sqlite3.Row) -> TraceEventRecord:
    """从 SQLite 行构造 TraceEventRecord。

    参数:
        row: trace_events 查询行。

    返回:
        TraceEventRecord。

    异常:
        json.JSONDecodeError: 如果 payload JSON 损坏。

    副作用:
        无。
    """

    return TraceEventRecord(
        event_id=row["event_id"],
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        span_id=row["span_id"] or "",
        parent_span_id=row["parent_span_id"] or "",
        sequence_no=row["sequence_no"],
        event_type=row["event_type"],
        source=row["source"],
        level=row["level"],
        payload=json.loads(row["payload_json"]),
        created_at=_from_text(row["created_at"]),
    )


def _span_from_row(row: sqlite3.Row) -> TraceSpanRecord:
    """从 SQLite 行构造 TraceSpanRecord。

    参数:
        row: trace_spans 查询行。

    返回:
        TraceSpanRecord。

    异常:
        json.JSONDecodeError: 如果 attributes 或 error JSON 损坏。

    副作用:
        无。
    """

    return TraceSpanRecord(
        span_id=row["span_id"],
        trace_id=row["trace_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        parent_span_id=row["parent_span_id"] or "",
        name=row["name"],
        kind=row["kind"],
        status=row["status"],
        attributes=json.loads(row["attributes_json"]),
        started_at=_from_text(row["started_at"]),
        ended_at=_from_text(row["ended_at"]) if row["ended_at"] else None,
        duration_ms=row["duration_ms"],
        error=json.loads(row["error_json"]) if row["error_json"] else None,
    )


def _to_text(value: datetime) -> str:
    """将 datetime 转换为 SQLite 文本。

    参数:
        value: datetime。

    返回:
        ISO 格式时间字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.astimezone(timezone.utc).isoformat()


def _from_text(value: str) -> datetime:
    """将 SQLite 时间文本转换为 datetime。

    参数:
        value: ISO 格式时间字符串。

    返回:
        datetime。

    异常:
        ValueError: 如果字符串不是合法 ISO 时间。

    副作用:
        无。
    """

    return datetime.fromisoformat(value)
