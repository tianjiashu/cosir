"""Trace span 记录值对象。

单一职责：承载一条 trace span 的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/trace`` 负责）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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
