"""Trace ledger 事件记录值对象。

单一职责：承载一条 trace event 的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/trace`` 负责）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.trace_infra.ids import new_event_id


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
