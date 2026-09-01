"""Process-local cancellation signals for running turns."""

from __future__ import annotations

import threading

from app.config.logging.logger import log


class TurnCancellationRegistry:
    """Track cancellation requests for turns running in the current process.

    本类只表达运行时控制信号，不是持久化事实来源。持久事实由 ``turns.status`` 与
    ``conversation_heads`` / ``conversation_changes`` 承载。
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory cancellation registry.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建线程锁和内存集合。
        """

        self._lock = threading.Lock()
        self._cancelled_turn_ids: set[str] = set()

    def mark_cancelled(self, turn_id: str) -> None:
        """Mark a turn as cancelled in the current process.

        参数:
            turn_id: 被请求取消的 turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入进程内取消集合，并记录调试日志。
        """

        with self._lock:
            self._cancelled_turn_ids.add(turn_id)
        log.info(
            "turn_cancellation_marked",
            extra={
                "msg": "turn cancellation signal marked",
                "data": {"turn_id": turn_id},
            },
        )

    def is_cancelled(self, turn_id: str) -> bool:
        """Return whether the turn has a process-local cancellation signal.

        参数:
            turn_id: 待检查的 turn 标识。

        返回:
            如果当前进程中已标记取消则为 True，否则为 False。

        异常:
            无。

        副作用:
            无。
        """

        with self._lock:
            return turn_id in self._cancelled_turn_ids

    def clear(self, turn_id: str) -> None:
        """Remove a turn cancellation signal.

        参数:
            turn_id: 待清理的 turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内取消集合移除对应标记。
        """

        with self._lock:
            self._cancelled_turn_ids.discard(turn_id)


cancellation_registry = TurnCancellationRegistry()
