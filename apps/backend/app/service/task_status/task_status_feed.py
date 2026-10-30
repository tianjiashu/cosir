"""Task 执行状态的进程内推送：侧栏、命令面板、页头与工作台标签共用的一条状态通道。

事实源是 ``ConversationRunModel.status``（经 Task 当前 Run 指针读取，见 ``TaskStatusQueries``）；
这里不保存状态，只在 Run 开始或状态变化时现读并转发给订阅者。漏掉的事件
（订阅与整表读取交错、进程内投递失败）由订阅端的周期对账收敛：流每隔一段时间重发整表，
不在这里为低概率时序叠加防御。
"""

from __future__ import annotations

import asyncio
from threading import Lock

from app.config.logging.logger import log
from app.storage.queries.task_status_queries import TaskStatusQueries
from app.storage.store_engines import main_session_factory

TaskStatuses = dict[int, str | None]


class TaskStatusSubscription:
    """一个订阅者待发送的状态变化；同一 Task 只留最新值，所以慢消费者不会积压。

    只在所属事件循环线程上读写（发布方经 ``call_soon_threadsafe`` 投递）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.changed = asyncio.Event()
        self._pending: TaskStatuses = {}

    def offer(self, statuses: TaskStatuses) -> None:
        self._pending.update(statuses)
        self.changed.set()

    def take(self) -> TaskStatuses:
        pending, self._pending = self._pending, {}
        self.changed.clear()
        return pending


class TaskStatusFeed:
    """进程级单例，由 ``app.service.depends.get_task_status_feed`` 装配。

    读库对象在第一次真正读取时才创建：投影路径（含不初始化存储的单测）每个 Run 事件都会调
    :meth:`publish`，没有订阅者时不应碰存储引擎。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._subscriptions: set[TaskStatusSubscription] = set()
        self._task_reads: TaskStatusQueries | None = None

    @property
    def _reads(self) -> TaskStatusQueries:
        if self._task_reads is None:
            self._task_reads = TaskStatusQueries(main_session_factory())
        return self._task_reads

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> TaskStatusSubscription:
        subscription = TaskStatusSubscription(loop)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: TaskStatusSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    def statuses(self) -> TaskStatuses:
        """当前全部 Task 的执行状态（整表帧与周期对账）。"""
        return self._reads.list_statuses()

    def publish(self, task_id: int) -> None:
        """Run 开始或状态变化后调用：现读该 Task 的执行状态并投给全部订阅者。

        可在任意线程调用。没有订阅者时不读库；Task 已被删除时不发（侧栏随目录移除它）。
        """
        with self._lock:
            subscriptions = tuple(self._subscriptions)
        if not subscriptions:
            return
        try:
            status = self._reads.status(task_id)
        except KeyError:
            return
        change = {task_id: status}
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(subscription.offer, change)
            except RuntimeError:
                # 订阅者的事件循环已关闭：连接正在收尾，交给它自己的 finally 退订。
                log.debug(
                    "task_status_subscriber_closed",
                    extra={"msg": "状态订阅者的事件循环已关闭", "data": {"task_id": task_id}},
                )
