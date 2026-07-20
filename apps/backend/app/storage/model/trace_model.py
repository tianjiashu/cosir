"""Trace SQLAlchemy model。"""

from sqlalchemy import Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class TraceEventModel(StorageBase):
    """`trace_events` 表模型。"""

    __tablename__ = "trace_events"
    __table_args__ = (
        Index("idx_trace_events_trace_sequence", "trace_id", "sequence_no"),
        Index("idx_trace_events_run_sequence", "run_id", "sequence_no"),
        Index("idx_trace_events_type_created", "event_type", "created_at"),
        UniqueConstraint("run_id", "sequence_no", name="idx_trace_events_run_sequence_unique"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    span_id: Mapped[str | None] = mapped_column(Text)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class TraceSpanModel(StorageBase):
    """`trace_spans` 表模型。"""

    __tablename__ = "trace_spans"
    __table_args__ = (
        Index("idx_trace_spans_trace_started", "trace_id", "started_at"),
        Index("idx_trace_spans_run_started", "run_id", "started_at"),
        Index("idx_trace_spans_status_started", "status", "started_at"),
    )

    span_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attributes_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_json: Mapped[str | None] = mapped_column(Text)
