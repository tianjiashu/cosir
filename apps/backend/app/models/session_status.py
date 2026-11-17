"""Session 状态的只读投影：执行状态与「等你操作」的调用数，两项是正交的事实。

执行状态是当前 Run 的 ``status``（经当前 Run 指针，不按创建时间猜），事实源是 ``RunModel.status``；
等你操作的事实源是 HITL 等待占位行（规则见 ``SessionContextRecord.awaits_user``）。Run 怎么走
（唤醒、落定、停止）不影响有没有事在等你。由 ``SessionCrud.get_status`` / ``list_statuses`` 读出，
供 Session 状态推送。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStatus:
    """无 Run 时 ``execution_status`` 为 None。"""

    execution_status: str | None
    awaiting_user_calls: int
