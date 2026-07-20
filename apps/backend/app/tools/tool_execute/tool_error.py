from app.tools.schemas import ToolObservation


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