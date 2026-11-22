"""Session 状态的只读投影：执行状态与「等你操作」的调用数，两项是正交的事实。

执行状态是当前 Run 的 ``status``（经当前 Run 指针，不按创建时间猜），事实源是 ``RunModel.status``；
等你操作在 ``SessionCrud.get_status`` / ``list_statuses`` 读出时只含 HITL 等待占位行（规则见
``SessionContextRecord.awaits_user``），``SessionFeed`` 推送前再合入进程内等待的终端批准。Run 怎么走
（唤醒、落定、停止）不影响有没有事在等你。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStatus:
    """无 Run 时 ``execution_status`` 为 None。"""

    execution_status: str | None
    awaiting_user_calls: int
