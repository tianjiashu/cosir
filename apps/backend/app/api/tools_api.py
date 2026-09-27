"""本机工具目录 API。"""

from fastapi import HTTPException

from app.app import app
from app.config.configuration import get_agent_registry, get_tool_registry
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tools.tool_grouping import DEFAULT_TOOL_GROUP


@app.get("/tools/groups")
async def get_tool_groups() -> dict[str, object]:
    """返回主 Agent 可用的注册工具，并按 ``ToolDefinition.group`` 聚合为目录。

    前端工具组选择器与配置中心共用本接口：调用方需要「分组 + 组内工具的名称与描述」（选择器显示
    每个分组的工具数量，配置页由分组反推工具名），因此返回分组目录而非分组名列表。

    返回:
        ``{"groups": [{"group": 分组名, "tools": [{"name": 工具名, "description": 描述}]}, ...]}``；
        其中 ``...`` 表示更多组内工具或更多分组。分组按组名字典序排列，组内工具按名称字典序排列；
        未声明分组的工具归入 ``其他工具``。

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
    groups: dict[str, list[dict[str, str]]] = {}
    for definition in definitions:
        groups.setdefault(definition.group or DEFAULT_TOOL_GROUP, []).append(
            {"name": definition.name, "description": definition.description}
        )
    return {
        "groups": [
            {"group": group, "tools": sorted(tools, key=lambda tool: tool["name"])}
            for group, tools in sorted(groups.items())
        ]
    }
