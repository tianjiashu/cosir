"""Durable Run State 的合法状态流转规则。"""

from typing import Dict, Set


class InvalidRunTransition(ValueError):
    """表示一次非法的运行状态流转。"""


class RunStateMachine:
    """校验 Durable Run State 的状态流转。"""

    _TRANSITIONS: Dict[str, Set[str]] = {
        "created": {"running", "waiting", "failed", "cancelled"},
        "pending": {"running", "waiting", "failed", "cancelled"},
        "running": {"waiting", "resuming", "interrupted", "completed", "failed", "cancelled", "needs_review"},
        "waiting": {"resuming", "cancelled", "failed"},
        "resuming": {"running", "waiting", "interrupted", "completed", "failed", "cancelled", "needs_review"},
        "interrupted": {"resuming", "needs_review", "cancelled", "failed"},
        "needs_review": {"resuming", "cancelled", "failed"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }

    def ensure_transition(self, current: str, target: str) -> None:
        """确认一次状态流转是否合法。

        参数:
            current: 当前运行状态。
            target: 目标运行状态。

        返回:
            无。

        异常:
            InvalidRunTransition: 当目标状态不允许从当前状态进入时抛出。

        副作用:
            无。
        """

        if current == target:
            return
        allowed = self._TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidRunTransition(f"invalid run transition: {current} -> {target}")
