"""Session 状态的只读投影：执行状态、两类「等你操作」的调用数与结果是否已读，几项是正交的事实。

执行状态是当前 Run 的 ``status``（经当前 Run 指针，不按创建时间猜），事实源是 ``RunModel.status``。
等你操作分两类、来源不同，不合成一个数：等你回答的是 HITL 等待占位行（规则见
``SessionContextRecord.awaits_user``，库里读出），等你批准的是进程内等待的终端批准（``SessionFeed``
推送前合入）。Run 怎么走（唤醒、落定、停止）不影响有没有事在等你。``unread`` 是
``SessionExtra.result_unread``。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStatus:
    """无 Run 时 ``execution_status`` 为 None；从库里读出时 ``awaiting_approvals`` 恒为 0（进程内事实）。"""

    execution_status: str | None
    awaiting_decisions: int
    awaiting_approvals: int
    unread: bool
