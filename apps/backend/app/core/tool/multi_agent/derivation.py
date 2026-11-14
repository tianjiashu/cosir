"""派生规则的唯一来源：哪几个工具会开出新 Session、建出什么类型、深度加几。

派生方式是封闭的小集合（委派、fork），每种恰好对应一个工具，所以在这里按工具名枚举，
不让工具在定义上再声明一遍。哪类 Session 允许哪种派生是 Session 一侧的规则（``models.session_kind``）；
深度上限不在派生入口拦，统一由 ``session_tool_scope`` 决定：新建 Session 固化时去掉，分叉在准入时拒绝。
"""

from dataclasses import dataclass

from app.core.tool.schemas import (
    TOOL_DELEGATE_TASK,
    TOOL_FORK_SESSION,
)
from app.models.session_kind import DerivationKind, SessionType
from app.models.session_record import SessionRecord


@dataclass(frozen=True)
class Derivation:
    """一种派生方式：发起它的工具、建出的 Session 类型，与新 Session 深度相对发起方的增量。"""

    tool: str
    creates: SessionType
    depth_step: int


DERIVATIONS: dict[DerivationKind, Derivation] = {
    "delegate": Derivation(tool=TOOL_DELEGATE_TASK, creates=SessionType.DELEGATED, depth_step=1),
    "fork": Derivation(tool=TOOL_FORK_SESSION, creates=SessionType.AGENT_FORK, depth_step=1),
}


def child_depth(parent: SessionRecord, kind: DerivationKind) -> int:
    """发起方 ``parent`` 以 ``kind`` 派生出的新 Session 的深度（写进 ``sessions.derivation_depth``）。

    用户新建会话与用户手动 fork 不是派生，深度为 0。
    """
    return parent.derivation_depth + DERIVATIONS[kind].depth_step

