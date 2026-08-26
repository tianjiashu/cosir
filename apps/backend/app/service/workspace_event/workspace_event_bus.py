"""Workspace 级状态事件广播总线。

单一职责：把 workspace 级状态事件（创建时的准备进度 preparing/ready/degraded，后续
可扩展其他 workspace 状态事件）按 ``workspace_id`` 分发到当前订阅者队列，供 workspace
级 SSE 端点推送。

与 turn 级 ``RuntimeEventBus`` 的差异（见 design §2.1）：
- 按 ``workspace_id`` 路由（而非 turn_id）；创建 workspace 时无 task/turn。
- 只承载 ``WorkspaceEvent``（不带 task/turn 信封），不落 turn 级事件表。
- ``publish`` 只向已注册订阅分发、无缓冲重放；``close`` 幂等且只关闭当前订阅桶，
  不写入永久关闭标记——关闭后重新 ``subscribe`` 可再次接收事件（对齐
  ``RuntimeEventBus.close_turn`` 语义，修复“二次 prepare 事件被吞”的 P1-7 bug）。

线程模型（P1-6 修复）：
- ``subscribe`` 在 SSE 端点的 async 上下文创建订阅队列，记录其所属 event loop。
- ``publish``/``close`` 可能来自工作线程（``prepare`` 经 ``asyncio.to_thread`` 运行）
  或 loop 线程（API 降级路径直接发布）。对非 loop 线程的发布，经
  ``loop.call_soon_threadsafe`` 调度到订阅队列所属 loop 线程执行，避免
  ``asyncio.Queue.put_nowait`` 跨线程违反 asyncio 契约（对齐
  ``RuntimeEventBus`` 的 ``_publish_runtime_event`` 跨线程模式）。
- 多订阅者可能分属不同 loop：每个订阅者独立记录 ``(queue, loop)``，逐订阅者判断
  当前线程归属后选择直发或跨线程投递，互不影响。

注：订阅者队列管理（``RLock`` + set 订阅表 + ``QueueFull`` 丢弃最旧 + 关闭哨兵）
与 ``RuntimeEventBus`` 同构。因路由键（workspace_id vs turn_id）与事件类型不同、
且 ``RuntimeEventBus`` 属已验收核心（改动有回归风险），本实现独立承载而暂不提取
公共订阅者注册表（改动最小化）；后续若出现第三个同类总线，应按 Rule of Three
提取公共基类。
"""

import asyncio
import threading
from contextlib import suppress
from dataclasses import dataclass

from app.config.logging.logger import log
from app.models.event.workspace_event import WorkspaceEvent
from app.service.workspace_event.workspace_event_subscription import (
    _QUEUE_CLOSED,
    WorkspaceEventSubscription,
)


@dataclass(frozen=True)
class _Subscriber:
    """一个 workspace 状态订阅者的队列与其所属 event loop。

    参数:
        queue: 进程内事件队列（订阅者消费）。
        loop: 队列创建时所在线程的 event loop；所有对该队列的写操作都必须调度到
            该 loop 线程执行（asyncio.Queue 非线程安全）。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变；可哈希以放入 set）。
    """

    queue: asyncio.Queue[WorkspaceEvent | object]
    loop: asyncio.AbstractEventLoop


class WorkspaceEventBus:
    """把 workspace 状态事件广播到订阅者。"""

    def __init__(self, queue_size: int = 256) -> None:
        """初始化空的 workspace 状态事件总线。

        参数:
            queue_size: 单个订阅者队列容量。

        返回:
            无。

        异常:
            ValueError: 当 queue_size 小于 1 时抛出。

        副作用:
            创建进程内订阅表。
        """

        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: dict[int, set[_Subscriber]] = {}
        self._lock = threading.RLock()

    def subscribe(self, workspace_id: int) -> WorkspaceEventSubscription:
        """订阅指定 workspace 的状态事件。

        参数:
            workspace_id: 需要订阅的 workspace 标识（int，单一事实类型）。

        返回:
            可异步迭代的 WorkspaceEventSubscription。

        异常:
            ValueError: 当 workspace_id 非正（<=0）时抛出。
            RuntimeError: 当调用线程没有正在运行的 event loop 时抛出（订阅必须
                在 async 上下文创建，SSE 端点满足此约束）。

        副作用:
            在进程内订阅表注册一个队列，并记录其所属 event loop 供跨线程投递。
        """

        if not workspace_id or workspace_id <= 0:
            raise ValueError("workspace_id must be a positive integer")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[WorkspaceEvent | object] = asyncio.Queue(maxsize=self._queue_size)
        subscriber = _Subscriber(queue=queue, loop=loop)
        with self._lock:
            self._subscribers.setdefault(workspace_id, set()).add(subscriber)
        log.info(
            "workspace_event_subscribed",
            extra={
                "msg": "workspace event subscriber registered",
                "data": {"workspace_id": workspace_id},
            },
        )
        return WorkspaceEventSubscription(workspace_id=workspace_id, queue=queue)

    def unsubscribe(self, subscription: WorkspaceEventSubscription) -> None:
        """移除一个 workspace 状态订阅。

        参数:
            subscription: 需要移除的订阅对象。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内订阅表移除该订阅者（含其 loop 记录）；若该 workspace 已无订阅
            则删除分桶。
        """

        with self._lock:
            subscribers = self._subscribers.get(subscription.workspace_id)
            if subscribers is None:
                return
            matched = next(
                (sub for sub in subscribers if sub.queue is subscription.queue),
                None,
            )
            if matched is not None:
                subscribers.discard(matched)
            if not subscribers:
                self._subscribers.pop(subscription.workspace_id, None)
        log.info(
            "workspace_event_unsubscribed",
            extra={
                "msg": "workspace event subscriber removed",
                "data": {"workspace_id": subscription.workspace_id},
            },
        )

    def publish(self, event: WorkspaceEvent) -> None:
        """发布一条状态事件到当前订阅者。

        参数:
            event: workspace 状态事件。

        返回:
            无。

        异常:
            无。订阅队列满时丢弃该订阅者最旧事件并记录日志；目标订阅者所属
            event loop 已关闭时记录警告并跳过（不抛出，避免阻断调用方）。

        副作用:
            把事件写入当前订阅者的内存队列；跨线程时经 ``call_soon_threadsafe``
            调度到订阅者所属 loop 线程执行。
        """

        with self._lock:
            subscribers = list(self._subscribers.get(event.workspace_id, set()))
        for subscriber in subscribers:
            self._dispatch(subscriber, event)

    def close(self, workspace_id: int) -> None:
        """关闭指定 workspace 的全部当前订阅（幂等）。

        参数:
            workspace_id: 需要关闭订阅的 workspace 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            向当前订阅队列写入关闭哨兵并清空该 workspace 的订阅桶；不写入永久
            关闭标记——此后再次 ``subscribe`` 同一 workspace 可正常接收后续事件
            （对齐 ``RuntimeEventBus.close_turn`` 语义）。
        """

        with self._lock:
            subscribers = list(self._subscribers.pop(workspace_id, set()))
        for subscriber in subscribers:
            self._dispatch(subscriber, _QUEUE_CLOSED)

    # ------------------------------------------------------------------
    # Internal: 跨线程投递
    # ------------------------------------------------------------------

    def _dispatch(self, subscriber: _Subscriber, item: WorkspaceEvent | object) -> None:
        """把事件/哨兵写入单个订阅者队列，跨线程时调度到其 loop 线程。

        参数:
            subscriber: 目标订阅者（含队列与其所属 loop）。
            item: 待写入的 WorkspaceEvent 或关闭哨兵。

        返回:
            无。

        异常:
            无。订阅者 loop 已关闭时 ``call_soon_threadsafe`` 抛 RuntimeError，
            记录警告并跳过该订阅者。

        副作用:
            若调用线程即订阅者 loop 线程则直接写队列；否则经
            ``loop.call_soon_threadsafe`` 投递到该 loop 线程执行。
        """

        if self._is_current_loop(subscriber.loop):
            self._put_or_drop_oldest(subscriber.queue, item)
            return
        try:
            subscriber.loop.call_soon_threadsafe(
                self._put_or_drop_oldest,
                subscriber.queue,
                item,
            )
        except RuntimeError:
            log.warning(
                "workspace_event_subscriber_loop_closed",
                extra={
                    "msg": "订阅者所属 event loop 已关闭，跳过投递",
                    "data": {"workspace_id": getattr(item, "workspace_id", None)},
                },
            )

    def _is_current_loop(self, loop: asyncio.AbstractEventLoop) -> bool:
        """判断调用线程是否为给定 loop 的所属线程。

        参数:
            loop: 需要判断的 event loop。

        返回:
            True 表示当前线程正运行该 loop；False 表示非 loop 线程（或没有
            正在运行的 loop）。

        异常:
            无。

        副作用:
            无。
        """

        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _put_or_drop_oldest(
        self,
        queue: asyncio.Queue[WorkspaceEvent | object],
        item: WorkspaceEvent | object,
    ) -> None:
        """向订阅队列写入一项；队列满时丢弃最旧事件后重放（运行于订阅者 loop 线程）。

        参数:
            queue: 订阅者事件队列。
            item: 待写入的 WorkspaceEvent 或关闭哨兵。

        返回:
            无。

        异常:
            无。队列满时先取走最旧事件再重放并记录警告。

        副作用:
            修改订阅队列内容；队列满时丢弃最旧事件。
        """

        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(item)
            log.warning(
                "workspace_event_subscriber_queue_full",
                extra={
                    "msg": "workspace event subscriber queue full; dropped oldest event",
                    "data": {"workspace_id": getattr(item, "workspace_id", None)},
                },
            )
