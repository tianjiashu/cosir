"""派生规则的唯一来源：哪几个工具会开出新 Task、建出什么类型、深度加几。

派生方式是封闭的小集合（委派、fork、建 Team），每种恰好对应一个工具，所以在这里按工具名枚举，
不让工具在定义上再声明一遍。哪类 Task 允许哪种派生是 Task 一侧的规则（``models.task_kind``）；
深度上限不在派生时拦，而是创建 Task 时由 ``task_tool_scope`` 一并决定：已到上限的 Task 拿不到
派生工具，模型看不见就不会调。
"""

from dataclasses import dataclass

from app.core.tool.schemas import (
    TOOL_AGENT_TEAM,
    TOOL_DELEGATE_TASK,
    TOOL_FORK_TASK,
)
from app.models.task_kind import DerivationKind
from app.models.task_record import TaskRecord


@dataclass(frozen=True)
class Derivation:
    """一种派生方式：发起它的工具、建出的 Task 类型，与新 Task 深度相对发起方的增量。"""

    tool: str
    creates: str
    depth_step: int


DERIVATIONS: dict[DerivationKind, Derivation] = {
    "delegate": Derivation(tool=TOOL_DELEGATE_TASK, creates="delegate_task", depth_step=1),
    "fork": Derivation(tool=TOOL_FORK_TASK, creates="fork", depth_step=1),
    # Team 是多个主 Agent 按固定流程协同，不是嵌套：节点与主 Task 同层，节点再委派出去才加深。
    "team": Derivation(tool=TOOL_AGENT_TEAM, creates="agent_team_node", depth_step=0),
}


def child_depth(parent: TaskRecord, kind: DerivationKind) -> int:
    """发起方 ``parent`` 以 ``kind`` 派生出的新 Task 的深度（写进 ``tasks.derivation_depth``）。

    用户新建对话与用户手动 fork 不是派生，深度为 0。
    """
    return parent.derivation_depth + DERIVATIONS[kind].depth_step

