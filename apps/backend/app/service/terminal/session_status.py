"""Terminal session lifecycle notifications shared by the local services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TerminalSessionStatusChange:
    """已确定的 terminal session 生命周期变化。

    该值对象只携带可安全投影到 Assistant Transport 的 session 身份和终态字段；
    不携带 PTY handle、输出内容或 worker 异常正文。
    """

    task_id: int
    run_id: int
    session_id: str
    generation: str
    status: str
    end_reason: str | None
    exit_code: int | None


class TerminalSessionStatusObserver(Protocol):
    """接收 terminal session 状态变化的窄观察接口。"""

    def __call__(self, change: TerminalSessionStatusChange) -> None:
        """消费一条已经由 TerminalSessionService 确认的状态变化。"""


class TerminalSessionStatusSource(Protocol):
    """为 Transport 冷重建提供当前进程 terminal 状态查询。"""

    def get_status_change(
        self,
        session_id: str,
        *,
        task_id: int,
        run_id: int,
    ) -> TerminalSessionStatusChange | None:
        """按精确 session locator 返回当前状态；未知 session 返回 ``None``。"""


TerminalSessionStatusObserverLike = Callable[[TerminalSessionStatusChange], None]


__all__ = [
    "TerminalSessionStatusChange",
    "TerminalSessionStatusObserver",
    "TerminalSessionStatusObserverLike",
    "TerminalSessionStatusSource",
]
