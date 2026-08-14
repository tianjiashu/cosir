"""Payload model for tool_call_finished events."""

from typing import Any, Literal

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallFinishedPayload(RuntimeEventPayload):
    """工具调用完成事件 payload。

    除标识与状态外，只携带**客户端渲染所需的事实数据**：成功时 ``content``
    （模型所见完整正文）与 ``data``（结构化载荷）；失败时 ``error``（主因：发生了
    什么）与 ``reason``（辅因：为什么失败 + 如何修正）、``retryable``（程序化重试
    信号）。后端不产出任何摘要文本或展示条目，渲染一律由客户端完成。
    """

    step_id: str
    tool_name: str
    status: Literal["success", "error", "cancelled"]
    tool_call_id: str
    content: str | None = None
    error: str = ""
    reason: str = ""
    retryable: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
