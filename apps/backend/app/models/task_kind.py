"""Task 类型规则：每类 Task 是不是用户能操作的对话、允许哪几种派生。

只是数据与纯规则，Task 一侧的唯一事实源。Task 创建时固化工具 schema，由
``core.tool.task_tool_scope.tools_for_task`` 据本表过滤：不是用户能操作的对话，就去掉需要用户
界面的工具（工具在 ``ToolDefinition`` 上声明 ``needs_user_surface``，需要用户作答的
``user_decision`` 工具自动算）；不允许的派生方式，去掉对应的派生工具（派生方式、工具名与深度增量
在 ``core.tool.derivation.DERIVATIONS``，本表不认识工具名）。不在运行期门禁再查一遍：门禁的上限
本来就是固化的 schema。

扩展：新增一类 Task，在 :data:`TASK_KINDS` 加一行；新增一种派生方式，在 :data:`DerivationKind`
与 ``DERIVATIONS`` 各加一项；新增一个需要用户界面的工具，只在它上面声明 ``needs_user_surface``。
"""

from dataclasses import dataclass
from typing import Literal

DerivationKind = Literal["delegate", "fork", "team"]


@dataclass(frozen=True)
class TaskKind:
    """一类 Task：是不是侧栏里用户能操作的对话，以及允许的派生方式。

    ``user_facing`` 为假的 Task（委派子 Agent、Team 节点）在前端只读：需要用户作答（审批、交接）或
    要在面板里操作（交互终端、配置草稿）的工具给了它也没人能用，调用后会永远等不到结果。
    """

    user_facing: bool
    derives: frozenset[DerivationKind]


_ALL_DERIVATIONS: frozenset[DerivationKind] = frozenset({"delegate", "fork", "team"})

TASK_KINDS: dict[str, TaskKind] = {
    "user": TaskKind(user_facing=True, derives=_ALL_DERIVATIONS),
    # 用户 fork 与 Agent fork 都是侧栏对话，能力与源对话一致（Agent fork 因此能沿用父 schema）。
    "fork": TaskKind(user_facing=True, derives=_ALL_DERIVATIONS),
    # fork 与建 Team 都要用户能操作的界面（Team 要审批，fork 产物是侧栏对话），只读 Task 只能委派。
    "delegate_task": TaskKind(user_facing=False, derives=frozenset({"delegate"})),
    "agent_team_node": TaskKind(user_facing=False, derives=frozenset({"delegate"})),
}

# 侧栏对话的类型：侧栏列表、标题支线、终端工作台等「只对用户能操作的对话开放」的规则都从这里取。
USER_FACING_TASK_TYPES: frozenset[str] = frozenset(
    task_type for task_type, kind in TASK_KINDS.items() if kind.user_facing
)

