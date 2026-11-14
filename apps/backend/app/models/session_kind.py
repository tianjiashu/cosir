"""Session 类型规则：每类 Session 是不是用户能操作的对话、允许哪几种派生。

只是数据与纯规则，Session 一侧的唯一事实源。新建 Session 固化工具 schema 时，由
``core.tool.session_tool_scope.tools_for_session`` 据本表过滤：不是用户能操作的对话，就去掉需要用户
界面的工具（工具在 ``ToolDefinition`` 上声明 ``needs_user_surface``，需要用户作答的
``user_decision`` 工具自动算）；不允许的派生方式，去掉对应的派生工具（派生方式、工具名与深度增量
在 ``core.tool.derivation.DERIVATIONS``，本表不认识工具名）。分叉沿用源的 schema，派生限制改由
运行期准入执行（``session_tool_scope.banned_derivation_tools``）。

扩展：新增一类 Session，在 :class:`SessionType` 和 :data:`SESSION_KINDS` 各加一项。新增派生方式，在
:data:`DerivationKind` 与 ``DERIVATIONS`` 各加一项。需要用户界面的工具声明 ``needs_user_surface``。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class SessionType(StrEnum):
    """会话类型的唯一值定义；数据库与 API 使用枚举的字符串值。"""

    USER = "user"
    USER_FORK = "user_fork"
    AGENT_FORK = "agent_fork"
    DELEGATED = "delegated"


FORK_SESSION_TYPES = frozenset({SessionType.USER_FORK, SessionType.AGENT_FORK})

DerivationKind = Literal["delegate", "fork"]


@dataclass(frozen=True)
class SessionKind:
    """一类 Session：是不是侧栏里用户能操作的对话，以及允许的派生方式。

    ``user_facing`` 为假的 Session（委派子 Agent）在前端只读：需要用户作答（审批、交接）或
    要在面板里操作（交互终端、配置草稿）的工具给了它也没人能用，调用后会永远等不到结果。
    """

    user_facing: bool
    derives: frozenset[DerivationKind]


_ALL_DERIVATIONS: frozenset[DerivationKind] = frozenset({"delegate", "fork"})

SESSION_KINDS: dict[SessionType, SessionKind] = {
    SessionType.USER: SessionKind(user_facing=True, derives=_ALL_DERIVATIONS),
    SessionType.USER_FORK: SessionKind(user_facing=True, derives=_ALL_DERIVATIONS),
    # Agent fork 不再分叉：继承的第一人称历史会让它接着做来源的请求，嵌套只会放大这种混淆。
    SessionType.AGENT_FORK: SessionKind(user_facing=True, derives=_ALL_DERIVATIONS - {"fork"}),
    # fork 要用户能操作的界面（产物是侧栏会话），只读 Session 只能委派。
    SessionType.DELEGATED: SessionKind(user_facing=False, derives=frozenset({"delegate"})),
}

# 侧栏会话的类型：侧栏列表、标题支线、终端工作台等「只对用户能操作的对话开放」的规则都从这里取。
USER_FACING_SESSION_TYPES: frozenset[SessionType] = frozenset(
    session_type for session_type, kind in SESSION_KINDS.items() if kind.user_facing
)
