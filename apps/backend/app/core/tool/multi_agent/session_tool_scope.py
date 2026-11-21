"""工具范围的唯一规则入口：按 Session 类型与派生深度决定哪些工具可用。

规则来自两张表：Session 一侧在 ``models.session_kind.SESSION_KINDS``，派生工具在 ``core.tool.derivation``。
去掉三类工具：需要用户界面、而这类 Session 不是用户能操作的对话的；这类 Session 不允许的派生方式对应的
派生工具；新 Session 深度已到上限时的全部派生工具。

新建 Session 没有可复用的请求前缀，用不了的工具在固化时去掉，免得模型调了才被拒。分叉要沿用源的
schema 才能命中源的缓存（工具列表是请求前缀的一部分），所以派生限制由运行期准入执行：
``RuntimeContext.for_run`` 按 :func:`banned_derivation_tools` 算出禁用集合，每次请求的准入名单
（``RequestTools``）据此拒绝，并经工具准入通知提前告知模型。

Session 类型与深度创建后不变；``AGENT_MAX_DERIVATION_DEPTH`` 每次现读，所以改上限对分叉的下一次 Run
即时生效，对 schema 已固化的 Session（委派子 Session）无效——那些工具的声明在创建时就去掉了。

选定的工具经 :func:`freeze_tool_surface` 固化成工具面（``SessionToolSurface``），延迟工具与 ``tool_search``
的搭配规则只在那里。
"""

import copy
from collections.abc import Iterable

from app.config.configuration import get_agent_registry, get_tool_registry
from app.config.settings import Settings
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tool.multi_agent.derivation import DERIVATIONS
from app.core.tool.schemas import TOOL_SEARCH, ToolDefinition
from app.models.session_kind import SESSION_KINDS, SessionType
from app.models.session_tool_surface import SessionToolSurface

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


def freeze_tool_surface(definitions: Iterable[ToolDefinition]) -> SessionToolSurface:
    """把一个 Session 选定的工具固化成工具面：全部定义深拷贝，记下哪些延迟加载。

    有延迟工具就一定带上 ``tool_search``：没有它，这些工具在这个 Session 里永远加载不了（Agent 配置没选
    它时由这里补上）。``tool_search`` 同时也是外部工具（MCP）的入口，所以有它的 Session 都能用 MCP。
    """
    selected = list(definitions)
    if any(d.deferred for d in selected) and all(d.name != TOOL_SEARCH for d in selected):
        tool_search = get_tool_registry().get_tool_definition(TOOL_SEARCH)
        if tool_search is None:
            raise RuntimeError("tool_search is not registered")  # 装配缺陷：注册表总是登记它
        selected.append(tool_search)
    return SessionToolSurface(
        schemas=[copy.deepcopy(d.to_model_tool_definition()) for d in selected],
        deferred=[d.name for d in selected if d.deferred],
    )


def main_agent_tool_surface(*, depth: int = 0) -> SessionToolSurface:
    """侧栏会话（主 Agent）该固化的工具面：主 Agent profile 选出的工具按类型与深度过滤，按注册顺序。

    异常:
        RuntimeError: 主 Agent profile 尚未装配。
    """
    profile = get_agent_registry().resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    if profile is None:
        raise RuntimeError("main agent profile is unavailable")
    selected = profile.select_tools(get_tool_registry().get_all_definitions())
    return freeze_tool_surface(tools_for_session(SessionType.USER, selected, depth=depth))


def banned_derivation_tools(session_type: SessionType, depth: int) -> frozenset[str]:
    """深度已到上限时全部派生工具，否则这类 Session 不允许的那几种；上限每次现读配置。"""
    if depth >= Settings.AGENT_MAX_DERIVATION_DEPTH:
        return _DERIVATION_TOOLS
    kind = SESSION_KINDS[session_type]
    return frozenset(rule.tool for name, rule in DERIVATIONS.items() if name not in kind.derives)
