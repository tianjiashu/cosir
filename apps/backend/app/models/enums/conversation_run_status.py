"""Conversation Run 生命周期状态机：状态枚举、命名迁移表与派生状态集合的唯一定义。

状态值即落库字符串。所有状态迁移只在 ``RunTransition`` 声明，由
``ConversationRunStateService`` 据此做一次条件更新；「活跃」「终态」等判断一律引用本模块的派生集合，
不在调用点手写状态组合。

Run 由 Task 的调度 actor（``TaskActor``）直接以 running 创建，没有「待认领」态。挂起有两种：Agent
自己挂起（等审批、等消息）是 ``suspended``，任何可唤醒消息都让它继续同一个 Run；用户挂起即取消，
是 ``cancelled``，只有用户能恢复。两者恢复规则不同，因此用状态区分。
"""

from enum import Enum, StrEnum


class ConversationRunStatus(StrEnum):
    """轮次执行状态；合法迁移见 ``RunTransition``。"""

    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_RUNNING = ConversationRunStatus.RUNNING
_SUSPENDED = ConversationRunStatus.SUSPENDED
_COMPLETED = ConversationRunStatus.COMPLETED
_FAILED = ConversationRunStatus.FAILED
_CANCELLED = ConversationRunStatus.CANCELLED

# 仍占用 Task 执行权的状态：同一 Task 同时至多一条。
ACTIVE_RUN_STATUSES = frozenset({_RUNNING, _SUSPENDED})
# 写入受控错误契约的终态。
ERROR_RUN_STATUSES = frozenset({_FAILED, _CANCELLED})
TERMINAL_RUN_STATUSES = frozenset({_COMPLETED}) | ERROR_RUN_STATUSES


class RunTransition(Enum):
    """Run 的全部合法迁移：来源状态集合、目标状态、是否清空上一次的终态结果。

    ``resets_result`` 表示重新进入 running 时清空结束原因、最终输出、用量与错误。
    """

    SUSPEND = (frozenset({_RUNNING}), _SUSPENDED, False)
    # 唤醒继续同一个 Run：挂起不产生终态结果，无需清空。
    WAKE = (frozenset({_SUSPENDED}), _RUNNING, False)
    RESUME = (frozenset({_CANCELLED, _FAILED}), _RUNNING, True)
    COMPLETE = (frozenset({_RUNNING}), _COMPLETED, False)
    FAIL = (frozenset({_RUNNING}), _FAILED, False)
    # 运行期取消、挂起态直接取消与重启收敛共用：挂起态没有执行者，由 actor 直接迁移。
    CANCEL = (frozenset({_RUNNING, _SUSPENDED}), _CANCELLED, False)

    def __init__(
        self,
        sources: frozenset[ConversationRunStatus],
        target: ConversationRunStatus,
        resets_result: bool,
    ) -> None:
        self.sources = sources
        self.target = target
        self.resets_result = resets_result
