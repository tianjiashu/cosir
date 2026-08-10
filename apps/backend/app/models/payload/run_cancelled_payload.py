"""Payload model for run_cancelled events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunCancelledPayload(RuntimeEventPayload):
    """运行取消事件 payload。

    取消是提前终止路径，但本轮已产生的 token 消耗必须回传给前端 / 排查侧（符合「可排查日志」铁律：
    异常 / 提前终止路径应让调用方拿到上下文）。token 字段与 :class:`RunFinishedPayload` 对齐，
    保证完成 / 失败 / 取消三态在前端 ``StatusBadge`` 的渲染逻辑统一；``model_node`` 的取消分支
    在 ``break`` 前已通过 ``TurnUsageStats`` 累积好计数，此处原样透传。
    """

    status: Literal["cancelled"]
    step_id: str | None = None
    error: str | None = None
    langfuse_trace_id: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
