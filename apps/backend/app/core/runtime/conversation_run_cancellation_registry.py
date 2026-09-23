"""Process-local cancellation signals for running turns."""

from __future__ import annotations

import threading

from app.config.logging.logger import log


class ConversationRunCancellationRegistry:
    """Track cancellation requests for turns running in the current process.

    本类只表达运行时控制信号，不是持久化事实来源。持久事实由 ConversationRun 状态
    与 canonical conversation facts 承载。
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
        self._cancelled_run_ids: set[int] = set()

    def mark_cancelled(self, run_id: int) -> None:
        """Mark a Conversation Run as cancelled in the current process.

        参数:
            run_id: 被请求取消的 Conversation Run 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入进程内取消集合，并记录一条 INFO 级日志（``turn_cancellation_marked``）。
        """

        with self._lock:
            self._cancelled_run_ids.add(run_id)
        log.info(
            "turn_cancellation_marked",
            extra={
                "msg": "run cancellation signal marked",
                "data": {"run_id": run_id},
            },
        )

    def is_cancelled(self, run_id: int | str) -> bool:
        """Return whether the Conversation Run has a process-local cancellation signal.

        参数:
            run_id: 待检查的 Conversation Run 标识。

        返回:
            如果当前进程中已标记取消则为 True，否则为 False。

        异常:
            无。

        副作用:
            无。
        """

        normalized_id = int(run_id) if isinstance(run_id, str) and run_id.isdigit() else run_id
        with self._lock:
            return normalized_id in self._cancelled_run_ids

    def clear(self, run_id: int) -> None:
        """Remove a Conversation Run cancellation signal.

        参数:
            run_id: 待清理的 Conversation Run 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内取消集合移除对应标记。
        """

        with self._lock:
            self._cancelled_run_ids.discard(run_id)


cancellation_registry = ConversationRunCancellationRegistry()
