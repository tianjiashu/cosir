"""工具范围的唯一规则入口：按 Session 类型与派生深度决定哪些工具可用。

规则来自两张表：Session 一侧在 ``models.session_kind.SESSION_KINDS``，派生工具在 ``core.tool.derivation``。
去掉三类工具：需要用户界面、而这类 Session 不是用户能操作的对话的；这类 Session 不允许的派生方式对应的
派生工具；新 Session 深度已到上限时的全部派生工具。

新建 Session 没有可复用的请求前缀，用不了的工具在固化时去掉，免得模型调了才被拒。分叉要沿用源的
schema 才能命中源的缓存（工具列表是请求前缀的一部分），所以派生限制由运行期准入执行：
``RuntimeContext.for_run`` 按 :func:`banned_derivation_tools` 拒绝，并经工具准入通知提前告知模型。

Session 类型与深度创建后不变；``AGENT_MAX_DERIVATION_DEPTH`` 每次现读，所以改上限对分叉的下一次 Run
即时生效，对 schema 已固化的 Session（委派子 Session）无效——那些工具的声明在创建时就去掉了。
"""

import copy
from collections.abc import Iterable

from app.config.configuration import get_agent_registry, get_tool_registry
from app.config.settings import Settings
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tool.multi_agent.derivation import DERIVATIONS
from app.core.tool.schemas import ToolDefinition
from app.models.session_kind import SESSION_KINDS, SessionType

ToolSchema = dict[str, object]

_DERIVATION_TOOLS = frozenset(rule.tool for rule in DERIVATIONS.values())


def tools_for_session(
    session_type: SessionType, definitions: Iterable[ToolDefinition], *, depth: int
) -> list[ToolDefinition]:
    """深度为 ``depth`` 的 ``session_type`` 类 Session 能拿到的工具，保持输入顺序。

    未登记的 ``session_type`` 是装配缺陷，``KeyError`` 直接上浮。
    """
    kind = SESSION_KINDS[session_type]
    banned = banned_derivation_tools(session_type, depth)
    return [
        definition
        for definition in definitions
        if (kind.user_facing or not definition.requires_user_surface)
        and definition.name not in banned
    ]


def main_agent_tool_schemas(*, depth: int = 0) -> list[ToolSchema]:
    """侧栏会话（主 Agent）该固化的模型工具 schema：主 Agent profile 选出的工具按类型与深度过滤。

    按工具注册顺序排列、深拷贝，调用方写进 ``sessions.tool_schemas``。

    异常:
        RuntimeError: 主 Agent profile 尚未装配。
    """
    profile = get_agent_registry().resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    if profile is None:
        raise RuntimeError("main agent profile is unavailable")
    selected = profile.select_tools(get_tool_registry().get_all_definitions())
    return [
        copy.deepcopy(tool.to_model_tool_definition())
        for tool in tools_for_session(SessionType.USER, selected, depth=depth)
    ]


def banned_derivation_tools(session_type: SessionType, depth: int) -> frozenset[str]:
    """深度已到上限时全部派生工具，否则这类 Session 不允许的那几种；上限每次现读配置。"""
    if depth >= Settings.AGENT_MAX_DERIVATION_DEPTH:
        return _DERIVATION_TOOLS
    kind = SESSION_KINDS[session_type]
    return frozenset(rule.tool for name, rule in DERIVATIONS.items() if name not in kind.derives)
