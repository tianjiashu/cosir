"""本机工具目录 API。"""

from fastapi import HTTPException

from app.app import app
from app.config.configuration import get_agent_registry, get_tool_registry
from app.core.agents.agent_profile_registry import AgentProfileRegistry


@app.get("/tools/groups")
async def get_tool_groups() -> dict[str, object]:
    """Return registered tools available to the main agent, grouped by ToolDefinition.group.

    The response contains sorted groups and each group's stable tool names and descriptions.
    It reads the initialized in-process profile and tool registry; it does not persist or mutate
    runtime state.

    Raises:
        HTTPException: the main-agent profile has not been initialized.
    """

    profile = get_agent_registry().resolve(
        AgentProfileRegistry.SYSTEM_WORKSPACE,
        "main_agent",
    )
    if profile is None:
        raise HTTPException(status_code=503, detail="main agent profile is unavailable")
    definitions = profile.select_tools(get_tool_registry().get_all_definitions())
    groups: dict[str, list[dict[str, object]]] = {}
    for definition in definitions:
        group = definition.group or "其他工具"
        groups.setdefault(group, []).append(
            {"name": definition.name, "description": definition.description}
        )
    return {"groups": [
        {"group": group, "tools": tools}
        for group, tools in sorted(groups.items())
    ]}
