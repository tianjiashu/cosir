"""Conversation Run 生命周期状态机：状态枚举、命名迁移表与派生状态集合的唯一定义。

状态值即落库字符串。所有状态迁移只在 ``RunTransition`` 声明，由
``ConversationRunStateService`` 据此做一次条件更新；「活跃」「终态」等判断一律引用本模块的派生集合，
不在调用点手写状态组合。

Run 由 ``WorkflowSession`` 在 Task 闸门内直接以 running 创建，没有「待认领」态。
"""

from enum import Enum, StrEnum


class ConversationRunStatus(StrEnum):
    """轮次执行状态；合法迁移见 ``RunTransition``。"""

    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_RUNNING = ConversationRunStatus.RUNNING
_WAITING = ConversationRunStatus.WAITING_FOR_INPUT
_COMPLETED = ConversationRunStatus.COMPLETED
_FAILED = ConversationRunStatus.FAILED
_CANCELLED = ConversationRunStatus.CANCELLED

# 仍占用 Task 执行权的状态：同一 Task 同时至多一条。
ACTIVE_RUN_STATUSES = frozenset({_RUNNING, _WAITING})
# 写入受控错误契约的终态。
ERROR_RUN_STATUSES = frozenset({_FAILED, _CANCELLED})
TERMINAL_RUN_STATUSES = frozenset({_COMPLETED}) | ERROR_RUN_STATUSES


class RunTransition(Enum):
    """Run 的全部合法迁移：来源状态集合、目标状态、是否清空上一次的终态结果。

    ``resets_result`` 表示重新进入 running 时清空结束原因、最终输出、用量与错误。
    """

    SUSPEND = (frozenset({_RUNNING}), _WAITING, False)
    RESUME_WITH_DECISION = (frozenset({_WAITING}), _RUNNING, True)
    RESUME = (frozenset({_CANCELLED, _FAILED}), _RUNNING, True)
    RESET_FOR_EDIT = (TERMINAL_RUN_STATUSES, _RUNNING, True)
    COMPLETE = (frozenset({_RUNNING}), _COMPLETED, False)
    FAIL = (frozenset({_RUNNING}), _FAILED, False)
    # 运行期取消与重启收敛共用：两者只差是否在外部事务内执行。
    CANCEL = (frozenset({_RUNNING}), _CANCELLED, False)

    def __init__(
        self,
        sources: frozenset[ConversationRunStatus],
        target: ConversationRunStatus,
        resets_result: bool,
    ) -> None:
        self.sources = sources
        self.target = target
        self.resets_result = resets_result
