"""Tool observation builders."""

from app.tools.schemas import ToolDefinition, ToolObservation


def tool_success(
    tool: ToolDefinition,
    content: str,
    tool_call_id: str = "",
) -> ToolObservation:
    """Build a successful tool observation."""

    return ToolObservation(
        tool_name=tool.name,
        status="success",
        content=content,
        permission=tool.permission,
        tool_call_id=tool_call_id,
    )


def tool_error(
    tool_name: str,
    error: str,
    reason: str,
    retryable: bool = False,
    permission: str = "",
    tool_call_id: str = "",
) -> ToolObservation:
    """Build an error tool observation."""

    return ToolObservation(
        tool_name=tool_name,
        status="error",
        content="",
        error=error,
        reason=reason,
        retryable=retryable,
        permission=permission,
        tool_call_id=tool_call_id,
    )
