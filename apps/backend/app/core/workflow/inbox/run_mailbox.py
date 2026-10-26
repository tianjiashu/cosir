"""Task 级的内部消息队列：外部队列投递来的消息与 Run 内通知，在下一次模型请求前取光。"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable

from app.core.workflow.inbox.task_message import TaskMessage


class RunMailbox:
    """投递通道，不是事实源：由 Task 的 actor 持有，每次开始执行时 :meth:`reset`，不跨重启。

    持久化消息的事实仍在外部队列，``prepare_input`` 并入时才确认删除；执行结束、取消或进程退出时
    未确认的消息留在外部队列，下次执行开始时重新投递。每次执行开始都清空：上一个 Run 没取完的
    Run 内通知（``id`` 为 ``None``）随之丢弃是有意的，下一个 Run 会重新产生自己的通知。

    出队按消息体的固定优先级（``PRIORITY``，通知在前），同优先级按投递顺序：用户输入、后台结果与
    决定之间不重排，保持对话时间线。需要绕过模型直接执行的决定由 prepare_input 经
    :meth:`take_first` 先挑出来——这条判定要查工具注册表，不放在这里。

    派发方在事件循环线程投递，``prepare_input`` 在工作线程取出，因此用锁保护；取出不阻塞。同一行
    在一次执行内只收一次：派发方每次都按外部队列全量投递，去重放在这里。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (优先级, 投递序号, 消息)：序号保证同优先级先进先出，且不比较消息本身。
        self._heap: list[tuple[int, int, TaskMessage]] = []
        self._order = itertools.count()
        # 本次执行已收过的外部队列行 id；Run 内通知没有 id，不去重。
        self._seen: set[int] = set()

    def reset(self) -> None:
        """开始一次新执行：丢弃残留消息与去重记录。"""
        with self._lock:
            self._heap.clear()
            self._seen.clear()

    def post(self, message: TaskMessage) -> None:
        with self._lock:
            if message.id is not None:
                if message.id in self._seen:
                    return
                self._seen.add(message.id)
            heapq.heappush(self._heap, (type(message.body).PRIORITY, next(self._order), message))

    def has_external(self) -> bool:
        """是否有来自外部队列、尚未取出的消息（``id`` 非空）；Run 内通知不算，不能唤醒挂起。"""
        with self._lock:
            return any(message.id is not None for _, _, message in self._heap)

    def take_first(self, predicate: Callable[[TaskMessage], bool]) -> TaskMessage | None:
        """按出队顺序取出第一条满足条件的消息，其余消息顺序不变；没有时返回 ``None``。"""
        with self._lock:
            for entry in sorted(self._heap):
                if predicate(entry[2]):
                    self._heap.remove(entry)
                    heapq.heapify(self._heap)
                    return entry[2]
            return None

    def pop(self) -> TaskMessage:
        with self._lock:
            return heapq.heappop(self._heap)[2]

    def isEmpty(self) -> bool:
        with self._lock:
            return not self._heap
