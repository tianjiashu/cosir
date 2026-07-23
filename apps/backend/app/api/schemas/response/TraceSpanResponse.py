from typing import Any

from pydantic import BaseModel


class TraceSpanResponse(BaseModel):
    """校验并序列化单条 trace span（与 ``TraceSpanRecord.to_dict()`` 对齐）。

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
        started_at: 开始时间 ISO 文本。
        ended_at: 结束时间 ISO 文本，可能为 None。
        duration_ms: 耗时毫秒，可能为 None。
        error: 错误摘要，可能为 None。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    span_id: str
    trace_id: str
    run_id: str
    task_id: str
    parent_span_id: str = ""
    name: str
    kind: str
    status: str
    attributes: dict[str, Any]
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    error: dict[str, Any] | None = None
