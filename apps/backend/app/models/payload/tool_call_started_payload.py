"""Payload model for reserved tool_call_started events."""

from typing import Any

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolCallStartedPayload(RuntimeEventPayload):
    """工具调用开始事件 payload。

    ``display`` 为工具的**静态展示声明**（verb / icon / expandable / expand_layout），
    不含任何后端渲染出的摘要文本；折叠态摘要由客户端按 ``arguments`` 渲染。
    """

    tool_name: str
    step_id: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
