"""Task 级进程内协作取消信号；不拥有持久状态、模型任务或工具生命周期。"""

from __future__ import annotations

import threading

from app.config.logging.logger import log


class ConversationRunCancellationRegistry:
    """记录当前进程内「某 Task 的当前执行应停止」的信号。

    按 task_id 而非 run_id 记录：同一 Task 内 Run 串行、同时只有一个执行者，停止的语义是
    「停下这个 Task 正在做的事」，命中的永远是当前执行，不受前端 run_id 滞后影响。

    本类只发信号、不做状态迁移：Agent 循环在 MODEL 检查点、工具在执行边界各自读取信号并
    收敛，Run 终态仍由 workflow 写入。信号生命周期由驱动方保证——只在 Task 存在活动执行时
    标记，执行释放时经 :meth:`consume` 原子取走（执行器还据此判断释放瞬间是否有迟到的取消），
    否则残留信号会让同一 Task 的下一次执行一启动就被取消。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled_task_ids: set[int] = set()

    def request_cancel(self, task_id: int) -> None:
        """记录取消意图；幂等，可从工具线程调用。"""
        with self._lock:
            if task_id in self._cancelled_task_ids:
                return
            self._cancelled_task_ids.add(task_id)
        log.info(
            "task_cancellation_requested",
            extra={"msg": "Task 取消信号已登记", "data": {"task_id": task_id}},
        )

    def is_cancelled(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self._cancelled_task_ids

    def consume(self, task_id: int) -> bool:
        """取走信号并返回取走前是否存在；只由执行释放边界调用。"""
        with self._lock:
            if task_id not in self._cancelled_task_ids:
                return False
            self._cancelled_task_ids.discard(task_id)
            return True


cancellation_registry = ConversationRunCancellationRegistry()
