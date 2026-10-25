"""一次执行的内部消息队列：外部队列投递来的消息与 Run 内通知，在下一次模型请求前取光。"""

from __future__ import annotations

import threading

from app.models.task_message import TaskMessage


class RunMailbox:
    """投递通道，不是事实源：随执行创建、随执行丢弃，不跨执行、不跨重启。

    持久化消息的事实仍在外部队列，``prepare_input`` 并入时才确认删除；执行结束、取消或进程退出
    丢掉本对象时，未确认的消息留在外部队列，由交接或下次投递重新送达。Run 内产生的通知（``id`` 为
    ``None``）只存在这里，随执行丢弃是有意的。

    派发方在事件循环线程投递，``prepare_input`` 在工作线程取出，因此用锁保护；取出不阻塞。同一行
    在一次执行内只收一次：派发方每次都按外部队列全量投递，去重放在这里。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[TaskMessage] = []
        self._seen: set[int] = set()

    def post(self, message: TaskMessage) -> None:
        with self._lock:
            if message.id is not None:
                if message.id in self._seen:
                    return
                self._seen.add(message.id)
            self._pending.append(message)

    def has_external(self) -> bool:
        """是否有来自外部队列、尚未取出的消息（``id`` 非空）；Run 内通知不算，不能唤醒挂起。"""
        with self._lock:
            return any(message.id is not None for message in self._pending)

    def drain(self) -> list[TaskMessage]:
        with self._lock:
            pending, self._pending = self._pending, []
            return pending
