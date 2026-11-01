"""交接冲突的工具结果：同一资源已交给用户时，第二次交接以受控错误返回给模型。"""

from app.core.tool.handoff.handoff_registry import HandoffConflictError
from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolObservation


def handoff_conflict_observation(tool_name: str, error: HandoffConflictError) -> ToolObservation:
    existing = error.existing
    return tool_error(
        tool_name,
        f"{existing.surface} {existing.resource_id} is already handed to the user by call "
        f"{existing.tool_call_id}; wait until they hand it back instead of handing it off again.",
        status_hint="已在交接中",
    )
