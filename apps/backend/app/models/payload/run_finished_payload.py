"""Payload model for run_finished events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunFinishedPayload(RuntimeEventPayload):
    """运行完成事件 payload。

    ``cost_cents`` 为估算成本（美分，设计文档阶段 5 ②）：由
    ``service/llm/cost_estimator.estimate_cost`` 按 litellm 价格表计算；
    未知模型 / 查询失败为 None（不估算）。
    """

    status: Literal["completed"]
    step_id: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    cost_cents: float | None = None
    langfuse_trace_id: str | None = None
