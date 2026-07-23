from typing import Any

from pydantic import BaseModel


class TraceEventResponse(BaseModel):
    """校验并序列化单条 trace event（与 ``TraceEventRecord.to_dict()`` 对齐）。

    参数:
        event_id: 事件主键。
        trace_id: Trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        span_id: 可选 span 标识。
        parent_span_id: 可选父 span 标识。
        sequence_no: 同一 run 内单调递增序号。
        event_type: canonical 事件类型。
        source: 事件来源模块。
        level: 事件等级。
        payload: 已脱敏 payload。
        created_at: 创建时间 ISO 文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    event_id: str
    trace_id: str
    run_id: str
    task_id: str
    span_id: str = ""
    parent_span_id: str = ""
    sequence_no: int
    event_type: str
    source: str
    level: str = "info"
    payload: dict[str, Any]
    created_at: str
