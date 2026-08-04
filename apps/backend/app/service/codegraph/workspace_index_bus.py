"""Workspace 级索引进度事件广播总线。

单一职责：把 workspace 创建时的索引进度事件（preparing/ready/degraded）按
``workspace_id`` 分发到当前订阅者队列，供 workspace 级 SSE 端点推送。

与 turn 级 ``RuntimeEventBus`` 的差异（见 design §2.1）：
- 按 ``workspace_id`` 路由（而非 turn_id）；创建 workspace 时无 task/turn。
- 只承载 ``WorkspaceIndexEvent``（不带 task/turn 信封），不落 turn 级事件表。
- ``publish`` 只向已注册订阅分发、无缓冲重放；``close`` 幂等（已关闭的
  workspace_id 再 publish 为 noop）。

注：订阅者队列管理（``RLock`` + set 订阅表 + ``QueueFull`` 丢弃最旧 + 关闭哨兵）
与 ``RuntimeEventBus`` 同构。因路由键（workspace_id vs turn_id）与事件类型不同、
且 ``RuntimeEventBus`` 属已验收核心（改动有回归风险），本实现独立承载而暂不提取
公共订阅者注册表（改动最小化）；后续若出现第三个同类总线，应按 Rule of Three
提取公共基类。
"""

import asyncio
import threading
from contextlib import suppress

from app.config.logging.logger import log
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.service.codegraph.workspace_index_subscription import (
    _QUEUE_CLOSED,
    WorkspaceIndexSubscription,
)


class WorkspaceIndexBus:
    """把 workspace 索引进度事件广播到订阅者。"""

    def __init__(self, queue_size: int = 256) -> None:
        """初始化空的 workspace 索引进度事件总线。

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
        self._subscribers: dict[str, set[asyncio.Queue[WorkspaceIndexEvent | object]]] = {}
        self._closed: set[str] = set()
        self._lock = threading.RLock()

    def subscribe(self, workspace_id: str) -> WorkspaceIndexSubscription:
        """订阅指定 workspace 的索引进度事件。

        参数:
            workspace_id: 需要订阅的 workspace 标识。

        返回:
            可异步迭代的 WorkspaceIndexSubscription。

        异常:
            ValueError: 当 workspace_id 为空时抛出。

        副作用:
            在进程内订阅表注册一个队列。
        """

        if not workspace_id:
            raise ValueError("workspace_id must be non-empty")
        queue: asyncio.Queue[WorkspaceIndexEvent | object] = asyncio.Queue(
            maxsize=self._queue_size
        )
        with self._lock:
            self._subscribers.setdefault(workspace_id, set()).add(queue)
        log.info(
            "workspace_index_subscribed",
            extra={
                "msg": "workspace index subscriber registered",
                "data": {"workspace_id": workspace_id},
            },
        )
        return WorkspaceIndexSubscription(workspace_id=workspace_id, queue=queue)

    def unsubscribe(self, subscription: WorkspaceIndexSubscription) -> None:
        """移除一个 workspace 索引进度订阅。

        参数:
            subscription: 需要移除的订阅对象。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内订阅表移除队列；若该 workspace 已无订阅则删除分桶。
        """

        with self._lock:
            queues = self._subscribers.get(subscription.workspace_id)
            if queues is None:
                return
            queues.discard(subscription.queue)
            if not queues:
                self._subscribers.pop(subscription.workspace_id, None)
        log.info(
            "workspace_index_unsubscribed",
            extra={
                "msg": "workspace index subscriber removed",
                "data": {"workspace_id": subscription.workspace_id},
            },
        )

    def publish(self, event: WorkspaceIndexEvent) -> None:
        """发布一条索引进度事件到当前订阅者。

        参数:
            event: workspace 索引进度事件。

        返回:
            无。

        异常:
            无。订阅队列满时丢弃该订阅者最旧事件并记录日志；已关闭的
            workspace_id 静默丢弃（noop）。

        副作用:
            把事件写入当前订阅者的内存队列。
        """

        with self._lock:
            if event.workspace_id in self._closed:
                return
            queues = list(self._subscribers.get(event.workspace_id, set()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(event)
                log.warning(
                    "workspace_index_subscriber_queue_full",
                    extra={
                        "msg": "workspace index subscriber queue full; dropped oldest event",
                        "data": {"workspace_id": event.workspace_id},
                    },
                )

    def close(self, workspace_id: str) -> None:
        """关闭指定 workspace 的全部订阅（幂等）。

        参数:
            workspace_id: 需要关闭订阅的 workspace 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            标记该 workspace 为已关闭（后续 publish 为 noop），并向当前订阅队列写入
            关闭哨兵。
        """

        with self._lock:
            self._closed.add(workspace_id)
            queues = list(self._subscribers.pop(workspace_id, set()))
        for queue in queues:
            try:
                queue.put_nowait(_QUEUE_CLOSED)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(_QUEUE_CLOSED)
