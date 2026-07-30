"""Payload model for tool_call_finished events."""

from typing import Any, Literal

from pydantic import Field

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallFinishedPayload(RuntimeEventPayload):
    """工具调用完成事件 payload。

    除标识与状态外，携带执行后渲染所需的结果字段：成功时 ``summary``（结果摘要，
    来自 ``ToolDisplayHints.render_result``）与 ``content``（模型所见完整正文）；
    失败时 ``error``（主因：发生了什么）与 ``reason``（辅因：为什么失败 + 如何修正）、
    ``retryable``（程序化重试信号）；``data`` 为结构化载荷（通用透传）。
    新字段均有默认值，旧事件（仅 4 字段）反序列化时自然取默认值。
    """

    step_id: str
    tool_name: str
    status: Literal["success", "error"]
    tool_call_id: str
    result_summary: dict[str, Any] | list[dict[str, Any]] | None = None
    summary: str | None = None
    content: str | None = None
    error: str = ""
    reason: str = ""
    retryable: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
