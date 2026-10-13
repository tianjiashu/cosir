"""上下文观测页的结构化消息响应。"""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from app.models.conversation_task_context import ConversationTaskContextRecord


class ContextMessageResponse(BaseModel):
    """一条按消息角色投影的上下文持久化记录。"""

    id: int | None
    task_id: int
    run_id: int | None
    sequence: int
    include_in_context: bool
    created_at: str
    updated_at: str
    role: str
    content: Any
    tool_calls: list[dict[str, Any]] | None = None
    invalid_tool_calls: list[dict[str, Any]] | None = None
    usage_metadata: dict[str, Any] | None = None
    tool_call_id: str | None = None
    status: str | None = None

    @classmethod
    def from_record(cls, record: ConversationTaskContextRecord) -> "ContextMessageResponse":
        """将持久化记录投影为前端使用的角色字段。"""

        message = record.message
        values: dict[str, Any] = {
            "id": record.id,
            "task_id": record.task_id,
            "run_id": record.run_id,
            "sequence": record.sequence,
            "include_in_context": record.include_in_context,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "content": message.content,
        }
        if isinstance(message, SystemMessage):
            values["role"] = "system"
        elif isinstance(message, HumanMessage):
            values["role"] = "user"
        elif isinstance(message, AIMessage):
            values.update(
                role="assistant",
                tool_calls=message.tool_calls or [],
                invalid_tool_calls=message.invalid_tool_calls or [],
                usage_metadata=message.usage_metadata,
            )
        elif isinstance(message, ToolMessage):
            metadata = record.transport_metadata or {}
            values.update(
                role="tool",
                tool_call_id=message.tool_call_id,
                status=metadata.get("status"),
            )
        else:
            values["role"] = message.type
        return cls(**values)
