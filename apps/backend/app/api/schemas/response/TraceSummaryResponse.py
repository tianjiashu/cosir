from pydantic import BaseModel, ConfigDict


class TraceSummaryResponse(BaseModel):
    """校验并序列化单条 trace 摘要（聚合自 ``trace_events`` 表）。

    仅声明稳定聚合列；其余列通过 ``extra="allow"`` 容忍，避免 SQL 聚合变动导致契约断裂。

    参数:
        trace_id: Trace 标识。
        task_id: 关联任务标识。
        run_id: 关联 run 标识。
        event_count: 事件总数。
        started_at: 最早事件时间文本。
        updated_at: 最近事件时间文本。

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
