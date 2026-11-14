"""本机工具目录 API。"""

from fastapi import HTTPException

from app.app import app
from app.config.configuration import get_agent_registry, get_tool_registry
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tool.tool_grouping import DEFAULT_TOOL_GROUP


@app.get("/tools/groups")
async def get_tool_groups() -> dict[str, object]:
    """返回主 Agent 可用工具的分组名（``ToolDefinition.group``），供配置中心勾选子 Agent 允许的工具组。

    只给组名：组名与工具名的换算由配置 API 在后端完成（``AgentConfigurationRequest``），前端不需要
    组内工具清单。

    返回:
        ``{"groups": [分组名, ...]}``，按组名字典序排列；未声明分组的工具归入 ``其他工具``。

    异常:
        HTTPException: 主 Agent profile 尚未初始化时抛出 503。

    副作用:
        无；只读进程内 profile 与工具注册表，不修改运行时状态。
    """

    profile = get_agent_registry().resolve(
        AgentProfileRegistry.SYSTEM_WORKSPACE,
        "main_agent",
    )
    if profile is None:
        raise HTTPException(status_code=503, detail="main agent profile is unavailable")
    definitions = profile.select_tools(get_tool_registry().get_all_definitions())
    return {"groups": sorted({definition.group or DEFAULT_TOOL_GROUP for definition in definitions})}
