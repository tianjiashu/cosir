"""单次工具调用的进程内取消信号；不拥有持久状态，也不决定工具调用终态。"""

from __future__ import annotations

import threading

from app.config.logging.logger import log


class ToolCallCancellationRegistry:
    """记录「某 Task 当前执行中的某次工具调用应停止」的信号。

    与 Task 级取消（``conversation_run_cancellation_registry``）职责不同：Task 级取消让整个当前
    执行停下，本注册表只中止**一次工具调用**，Run 与 Agent 在其后继续。同一批并行工具调用中只有
    被点名的那一个停止。

    键是 ``(task_id, tool_call_id)``，与 Task 级取消一样按 Task 而不是 Run 寻址：同一 Task 同时
    只有一个执行者，命中的永远是当前执行，HTTP 入口也不必先把 Task 解析成 run_id。信号生命周期
    由执行方保证：单次调用结束时经 :meth:`clear` 释放，执行收尾时经 :meth:`clear_task` 兜底回收
    「点名了已结束的调用」这类不会再被消费的信号，否则会命中下一个执行里恰好同名的调用。

    本类只表达运行时控制信号：工具是否真的被中断、调用终态，仍由工具执行层产出的
    ``ToolObservation`` 与 Run 事实承载。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled_calls: set[tuple[int, str]] = set()

    def mark_cancelled(self, task_id: int, tool_call_id: str) -> bool:
        """记录取消意图；返回是否为新标记（已标记过时为 ``False``）。可从工具线程调用。"""
        key = (task_id, tool_call_id)
        with self._lock:
            if key in self._cancelled_calls:
                return False
            self._cancelled_calls.add(key)
        log.info(
            "tool_call_cancellation_marked",
            extra={
                "msg": "工具调用取消信号已登记",
                "data": {"task_id": task_id, "tool_call_id": tool_call_id},
            },
        )
        return True

    def is_cancelled(self, task_id: int, tool_call_id: str) -> bool:
        with self._lock:
            return (task_id, tool_call_id) in self._cancelled_calls

    def clear(self, task_id: int, tool_call_id: str) -> None:
        """单次调用结束时释放它的信号；重复清理安全。"""
        with self._lock:
            self._cancelled_calls.discard((task_id, tool_call_id))

    def clear_task(self, task_id: int) -> None:
        """执行收尾时回收该 Task 遗留的全部工具级信号；只记清理条数，不记调用 id 列表。"""
        with self._lock:
            removed = {key for key in self._cancelled_calls if key[0] == task_id}
            self._cancelled_calls -= removed
        if removed:
            log.info(
                "tool_call_cancellation_task_cleared",
                extra={
                    "msg": "执行收尾时清理了遗留的工具级取消信号",
                    "data": {"task_id": task_id, "cleared_count": len(removed)},
                },
            )


tool_call_cancellation_registry = ToolCallCancellationRegistry()
