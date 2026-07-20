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