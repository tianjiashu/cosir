"""本机服务共享的 Task 终端生命周期通知契约。

只保留状态变化值对象与观察者函数类型。观察者与状态查询不再各维护一套 ``Protocol``
影子接口：观察者本身就是装配层传入的可调用对象（见 ``TerminalSessionStatusObserverLike``），
状态查询由真实实现 ``TerminalSessionService.get_status_change`` 承担。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalSessionStatusChange:
    """已确定的 terminal session 生命周期变化。

    该值对象只携带可安全投影到 Assistant Transport 的 session 身份和终态字段；
    不携带 PTY handle、输出内容或 worker 异常正文。
    """

    task_id: int
    session_id: str
    generation: str
    status: str
    end_reason: str | None
    exit_code: int | None


TerminalSessionStatusObserverLike = Callable[[TerminalSessionStatusChange], None]


__all__ = [
    "TerminalSessionStatusChange",
    "TerminalSessionStatusObserverLike",
]
