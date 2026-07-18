"""Trace CRUD。"""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import asc, delete, desc, func, select

from app.core.trace.records import TraceEventRecord, TraceSpanRecord
from app.storage.database import create_session_factory
from app.storage.model.trace import TraceEventModel, TraceSpanModel
from app.storage.schema import initialize_app_schema


class TraceStore:
    """读写 trace_events 与 trace_spans。"""

    _sequence_locks: dict[Path, Lock] = {}
    _sequence_locks_guard = Lock()

    def __init__(self, database_path: Path) -> None:
        """初始化 Trace 存储并确保 schema 存在。"""

        self._database_path = database_path.resolve()
        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)
        self._sequence_lock = self._lock_for_database(self._database_path)

    def close(self) -> None:
        """Dispose the SQLite engine held by this store.

        Parameters:
            None.

        Returns:
            None.

        Raises:
            None.

        Side effects:
            Closes pooled SQLite connections so the database file can be removed on Windows.
        """

        self._engine.dispose()

    def next_sequence(self, run_id: str) -> int:
        """返回同一 run 内下一个 trace event 序号。"""

        with self._session_factory() as session:
            value = session.execute(select(func.coalesce(func.max(TraceEventModel.sequence_no), 0) + 1).where(TraceEventModel.run_id == run_id)).scalar_one()
        return int(value)

    def append_event(self, event: TraceEventRecord) -> TraceEventRecord:
        """追加一个 trace ledger 事件。"""

        with self._sequence_lock:
            with self._session_factory.begin() as session:
                sequence_no = int(session.execute(select(func.coalesce(func.max(TraceEventModel.sequence_no), 0) + 1).where(TraceEventModel.run_id == event.run_id)).scalar_one())
                assigned = replace(event, sequence_no=sequence_no)
                session.add(_event_model(assigned))
        return assigned

    def start_span(self, span: TraceSpanRecord) -> None:
        """写入一个开始状态的 span。"""

        with self._session_factory.begin() as session:
            session.add(_span_model(span))

    def finish_span(self, span_id: str, status: str, ended_at: datetime | None = None, error: dict[str, Any] | None = None) -> None:
        """结束一个 span。"""

        finished_at = ended_at or datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            row = session.get(TraceSpanModel, span_id)
            if row is None:
                raise KeyError(span_id)
            duration_ms = int((finished_at - _from_text(row.started_at)).total_seconds() * 1000)
            row.status = status
            row.ended_at = _to_text(finished_at)
            row.duration_ms = duration_ms
            row.error_json = json.dumps(error, ensure_ascii=False) if error else None

    def list_events(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[TraceEventRecord]:
        """查询 trace event。"""

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        statement = select(TraceEventModel)
        if trace_id:
            statement = statement.where(TraceEventModel.trace_id == trace_id)
        if run_id:
            statement = statement.where(TraceEventModel.run_id == run_id)
        with self._session_factory() as session:
            rows = session.execute(statement.order_by(asc(TraceEventModel.run_id), asc(TraceEventModel.sequence_no), asc(TraceEventModel.created_at)).limit(limit)).scalars().all()
        return [_event_from_model(row) for row in rows]

    def list_trace_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回 trace 摘要列表。"""

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    TraceEventModel.trace_id,
                    func.min(TraceEventModel.task_id).label("task_id"),
                    func.min(TraceEventModel.run_id).label("run_id"),
                    func.count().label("event_count"),
                    func.min(TraceEventModel.created_at).label("started_at"),
                    func.max(TraceEventModel.created_at).label("updated_at"),
                )
                .group_by(TraceEventModel.trace_id)
                .order_by(desc("updated_at"))
                .limit(limit)
            ).mappings().all()
        return [dict(row) for row in rows]

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """返回单个 trace 摘要。"""

        with self._session_factory() as session:
            row = session.execute(
                select(
                    TraceEventModel.trace_id,
                    func.min(TraceEventModel.task_id).label("task_id"),
                    func.min(TraceEventModel.run_id).label("run_id"),
                    func.count().label("event_count"),
                    func.min(TraceEventModel.created_at).label("started_at"),
                    func.max(TraceEventModel.created_at).label("updated_at"),
                )
                .where(TraceEventModel.trace_id == trace_id)
                .group_by(TraceEventModel.trace_id)
            ).mappings().first()
        if row is None:
            raise KeyError(trace_id)
        return dict(row)

    def list_spans(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[TraceSpanRecord]:
        """查询 trace span。"""

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        statement = select(TraceSpanModel)
        if trace_id:
            statement = statement.where(TraceSpanModel.trace_id == trace_id)
        if run_id:
            statement = statement.where(TraceSpanModel.run_id == run_id)
        with self._session_factory() as session:
            rows = session.execute(statement.order_by(asc(TraceSpanModel.started_at)).limit(limit)).scalars().all()
        return [_span_from_model(row) for row in rows]

    def delete_by_task_ids(self, task_ids: list[str]) -> None:
        """删除任务集合下的 trace events 与 spans。

        参数:
            task_ids: 需要删除的任务标识符列表。

        返回:
            无。

        异常:
            无。

        副作用:
            删除 trace_events 与 trace_spans 中的关联记录。
        """

        if not task_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TraceEventModel).where(TraceEventModel.task_id.in_(tuple(task_ids))))
            session.execute(delete(TraceSpanModel).where(TraceSpanModel.task_id.in_(tuple(task_ids))))

    @classmethod
    def _lock_for_database(cls, database_path: Path) -> Lock:
        """返回同一 SQLite 文件共享的 trace sequence 分配锁。"""

        with cls._sequence_locks_guard:
            if database_path not in cls._sequence_locks:
                cls._sequence_locks[database_path] = Lock()
            return cls._sequence_locks[database_path]


def _to_text(value: datetime) -> str:
    """将 datetime 转换为 UTC ISO 文本。"""

    return value.astimezone(timezone.utc).isoformat()


def _from_text(value: str) -> datetime:
    """将 ISO 文本转换为 datetime。"""

    return datetime.fromisoformat(value)


def _event_model(event: TraceEventRecord) -> TraceEventModel:
    """将 trace event 记录转换为 model。"""

    return TraceEventModel(event_id=event.event_id, trace_id=event.trace_id, run_id=event.run_id, task_id=event.task_id, span_id=event.span_id, parent_span_id=event.parent_span_id, sequence_no=event.sequence_no, event_type=event.event_type, source=event.source, level=event.level, payload_json=json.dumps(event.payload, ensure_ascii=False), created_at=_to_text(event.created_at))


def _event_from_model(row: TraceEventModel) -> TraceEventRecord:
    """将 trace event model 转换为记录。"""

    return TraceEventRecord(
        trace_id=row.trace_id,
        run_id=row.run_id,
        task_id=row.task_id,
        event_type=row.event_type,
        source=row.source,
        payload=json.loads(row.payload_json),
        sequence_no=row.sequence_no,
        event_id=row.event_id,
        span_id=row.span_id or "",
        parent_span_id=row.parent_span_id or "",
        level=row.level,
        created_at=_from_text(row.created_at),
    )


def _span_model(span: TraceSpanRecord) -> TraceSpanModel:
    """将 trace span 记录转换为 model。"""

    return TraceSpanModel(span_id=span.span_id, trace_id=span.trace_id, run_id=span.run_id, task_id=span.task_id, parent_span_id=span.parent_span_id, name=span.name, kind=span.kind, status=span.status, started_at=_to_text(span.started_at), ended_at=_to_text(span.ended_at) if span.ended_at else None, duration_ms=span.duration_ms, attributes_json=json.dumps(span.attributes, ensure_ascii=False), error_json=json.dumps(span.error, ensure_ascii=False) if span.error else None)


def _span_from_model(row: TraceSpanModel) -> TraceSpanRecord:
    """将 trace span model 转换为记录。"""

    return TraceSpanRecord(row.span_id, row.trace_id, row.run_id, row.task_id, row.parent_span_id or "", row.name, row.kind, row.status, json.loads(row.attributes_json), _from_text(row.started_at), _from_text(row.ended_at) if row.ended_at else None, row.duration_ms, json.loads(row.error_json) if row.error_json else None)
