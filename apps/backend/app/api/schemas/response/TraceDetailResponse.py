from typing import Any

from pydantic import BaseModel, ConfigDict


class TraceDetailResponse(BaseModel):
    """校验并序列化单条 trace 详情（摘要 + events/spans/logs）。

    详情由 ``TraceQueryService.get_trace_summary`` 聚合得到，含摘要列与三项列表；
    其余键通过 ``extra="allow"`` 容忍。列表项保留为 ``dict``，避免对 observability
    原始结构过度建模而漂移。

    参数:
        trace_id: Trace 标识。
        task_id: 关联任务标识。
        run_id: 关联 run 标识。
        event_count: 事件总数。
        started_at: 最早事件时间文本。
        updated_at: 最近事件时间文本。
        events: 该 trace 的 event 字典列表。
        spans: 该 trace 的 span 字典列表。
        logs: 该 trace 的日志字典列表。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(extra="allow")

    trace_id: str
    task_id: str
    run_id: str
    event_count: int
    started_at: str
    updated_at: str
    events: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    logs: list[dict[str, Any]]
