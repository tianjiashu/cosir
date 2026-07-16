"""Trace Backbone 可持久化记录。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.trace.ids import new_event_id


@dataclass(frozen=True)
class TraceEventRecord:
    """Trace ledger 事件记录。

    参数:
        event_id: 事件主键。
        trace_id: Trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        event_type: canonical 事件类型。
        source: 事件来源模块。
        payload: 已脱敏 payload。
        sequence_no: 同一 run 内单调递增序号。
        span_id: 可选 span 标识。
        parent_span_id: 可选父 span 标识。
        level: 事件等级。
        created_at: UTC 创建时间。

    返回:
        不可变 trace event 记录。

    异常:
        无。

    副作用:
        使用默认值时生成事件 ID 和时间戳。
    """

    trace_id: str
    run_id: str
    task_id: str
    event_type: str
    source: str
    payload: dict[str, Any]
    sequence_no: int
    event_id: str = field(default_factory=new_event_id)
    span_id: str = ""
    parent_span_id: str = ""
    level: str = "info"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            包含 ISO 时间字符串的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sequence_no": self.sequence_no,
            "event_type": self.event_type,
            "source": self.source,
            "level": self.level,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class TraceSpanRecord:
    """Trace span 记录。

    参数:
        span_id: Span 标识。
        trace_id: Trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        parent_span_id: 父 span 标识。
        name: span 名称。
        kind: span 类型。
        status: span 状态。
        attributes: 已脱敏属性。
        started_at: 开始时间。
        ended_at: 结束时间。
        duration_ms: 耗时毫秒。
        error: 错误摘要。

    返回:
        不可变 span 记录。

    异常:
        无。

    副作用:
        默认 started_at 会读取系统时钟。
    """

    span_id: str
    trace_id: str
    run_id: str
    task_id: str
    parent_span_id: str
    name: str
    kind: str
    status: str
    attributes: dict[str, Any]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    duration_ms: int | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            包含时间字符串与 attributes 的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "attributes": self.attributes,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

