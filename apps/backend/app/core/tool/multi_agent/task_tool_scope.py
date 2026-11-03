"""Task 创建时固化工具 schema 的唯一规则入口：按 Task 类型与派生深度决定给哪些工具。

规则来自两张表：Task 一侧在 ``models.task_kind.TASK_KINDS``，派生工具在 ``core.tool.derivation``。
去掉三类工具：需要用户界面、而这类 Task 不是用户能操作的对话的；这类 Task 不允许的派生方式对应的
派生工具；新 Task 深度已到上限时的全部派生工具。

深度也在这里而不在派生入口检查：用不了的工具不给模型看，免得它调了才被拒、白费一步。Task 深度在
创建时定下、之后不变，所以只判断一次；代价是改上限只影响之后新建的 Task，与其余工具配置同一口径。
只在固化时生效：运行期准入门禁的上限就是固化的 schema，不在那里再判一次。
"""

import copy
from collections.abc import Iterable

from app.config.configuration import get_agent_registry, get_tool_registry
from app.config.settings import Settings
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tool.multi_agent.derivation import DERIVATIONS
from app.core.tool.schemas import ToolDefinition
from app.models.task_kind import TASK_KINDS

ToolSchema = dict[str, object]

_DERIVATION_TOOLS = frozenset(rule.tool for rule in DERIVATIONS.values())


def tools_for_task(
    task_type: str, definitions: Iterable[ToolDefinition], *, depth: int
) -> list[ToolDefinition]:
    """深度为 ``depth`` 的 ``task_type`` 类 Task 能拿到的工具，保持输入顺序。

    未登记的 ``task_type`` 是装配缺陷，``KeyError`` 直接上浮。
    """
    kind = TASK_KINDS[task_type]
    banned = _banned_derivation_tools(task_type, depth)
    return [
        definition
        for definition in definitions
        if (kind.user_facing or not definition.requires_user_surface)
        and definition.name not in banned
    ]


def main_agent_tool_schemas(*, depth: int = 0) -> list[ToolSchema]:
    """侧栏对话（主 Agent）该固化的模型工具 schema：主 Agent profile 选出的工具按类型与深度过滤。

    按工具注册顺序排列、深拷贝，调用方写进 ``tasks.tool_schemas``。

    异常:
        RuntimeError: 主 Agent profile 尚未装配。
    """
    profile = get_agent_registry().resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    if profile is None:
        raise RuntimeError("main agent profile is unavailable")
    selected = profile.select_tools(get_tool_registry().get_all_definitions())
    return [
        copy.deepcopy(tool.to_model_tool_definition())
        for tool in tools_for_task("user", selected, depth=depth)
    ]


def fork_tool_schemas(
    source: list[ToolSchema], *, source_depth: int, depth: int
) -> list[ToolSchema]:
    """分叉的工具 schema：沿用源 Task 的（请求前缀与源一致，复用缓存），只按新深度重算派生工具。

    Agent 分叉比源深一层，可能正好到上限，去掉派生工具。只有比源浅的分叉（用户手动分叉深度归 0，
    源是更深的 Agent 分叉）才可能缺少源当时因深度被去掉的派生工具，按新建侧栏对话的规则从注册表
    补到末尾；不比源浅时源缺的派生工具是 Agent 配置没选，不补。常见情况原样复制。
    """
    banned = _banned_derivation_tools("fork", depth)
    kept = [copy.deepcopy(schema) for schema in source if schema["name"] not in banned]
    wanted = _DERIVATION_TOOLS - banned - {str(schema["name"]) for schema in kept}
    if depth >= source_depth or not wanted:
        return kept
    return kept + [
        schema for schema in main_agent_tool_schemas(depth=depth) if schema["name"] in wanted
    ]


def _banned_derivation_tools(task_type: str, depth: int) -> frozenset[str]:
    """深度已到上限时全部派生工具，否则这类 Task 不允许的那几种；上限每次现读配置。"""
    if depth >= Settings.AGENT_MAX_DERIVATION_DEPTH:
        return _DERIVATION_TOOLS
    kind = TASK_KINDS[task_type]
    return frozenset(rule.tool for name, rule in DERIVATIONS.items() if name not in kind.derives)
