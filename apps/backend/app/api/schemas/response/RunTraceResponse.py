from typing import Any

from pydantic import BaseModel, ConfigDict


class RunTraceResponse(BaseModel):
    """校验并序列化 run 对应的 trace 摘要（events/spans/logs）。

    由 ``TraceQueryService.get_run_trace`` 聚合得到；其余键通过 ``extra="allow"`` 容忍。

    参数:
        run_id: Durable Run 标识。
        trace_id: 关联 Trace 标识。
        events: event 字典列表。
        spans: span 字典列表。
        logs: 日志字典列表。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    trace_id: str
    events: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    logs: list[dict[str, Any]]
