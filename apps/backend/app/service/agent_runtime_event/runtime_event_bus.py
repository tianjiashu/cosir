"""In-process runtime event broadcast bus."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from app.config.logging.logger import log
from app.models.runtime_event import RuntimeEvent
from app.service.agent_runtime_event.runtime_event_subscription import (
    _QUEUE_CLOSED,
    RuntimeEventSubscription,
)


class RuntimeEventBus:
    """Broadcast runtime events to in-process turn subscribers."""

    def __init__(self, queue_size: int = 256) -> None:
        """Initialize an empty in-process event bus.

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
        self._subscribers: dict[str, set[asyncio.Queue[RuntimeEvent | object]]] = {}
        self._published_event_ids_by_turn: dict[str, set[str]] = {}
        self._producer_turn_ids: set[str] = set()
        self._lock = threading.RLock()

    def claim_turn_producer(self, turn_id: str) -> bool:
        """Claim the single active producer slot for a turn.

        参数:
            turn_id: 需要启动 producer 的 turn 标识。

        返回:
            成功抢占 producer 槽位时返回 True；已有 producer 时返回 False。

        异常:
            ValueError: 当 turn_id 为空时抛出。

        副作用:
            成功时记录该 turn 已有活跃 producer。
        """

        if not turn_id:
            raise ValueError("turn_id must be non-empty")
        with self._lock:
            if turn_id in self._producer_turn_ids:
                return False
            self._producer_turn_ids.add(turn_id)
            return True

    def release_turn_producer(self, turn_id: str) -> None:
        """Release the active producer slot for a turn.

        参数:
            turn_id: 需要释放 producer 槽位的 turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            移除该 turn 的活跃 producer 标记。
        """

        with self._lock:
            self._producer_turn_ids.discard(turn_id)

    def subscribe(self, turn_id: str) -> RuntimeEventSubscription:
        """Subscribe to runtime events for a turn.

        参数:
            turn_id: 需要订阅的 turn 标识。

        返回:
            可异步迭代的 RuntimeEventSubscription。

        异常:
            ValueError: 当 turn_id 为空时抛出。

        副作用:
            在进程内订阅表注册一个队列。
        """

        if not turn_id:
            raise ValueError("turn_id must be non-empty")
        queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.setdefault(turn_id, set()).add(queue)
        log.info(
            "runtime_event_subscribed",
            extra={"msg": "runtime event subscriber registered", "data": {"turn_id": turn_id}},
        )
        return RuntimeEventSubscription(turn_id=turn_id, queue=queue)

    def unsubscribe(self, subscription: RuntimeEventSubscription) -> None:
        """Remove a runtime event subscription.

        参数:
            subscription: 需要移除的订阅对象。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内订阅表移除队列。
        """

        with self._lock:
            queues = self._subscribers.get(subscription.turn_id)
            if queues is None:
                return
            queues.discard(subscription.queue)
            if not queues:
                self._subscribers.pop(subscription.turn_id, None)
        log.info(
            "runtime_event_unsubscribed",
            extra={
                "msg": "runtime event subscriber removed",
                "data": {"turn_id": subscription.turn_id},
            },
        )

    def publish(self, event: RuntimeEvent) -> None:
        """Publish an event to current subscribers of its turn.

        参数:
            event: 已持久化或实时产生的 runtime event。

        返回:
            无。

        异常:
            无。重复 event_id 会被忽略；订阅队列满时丢弃该订阅者最旧事件并记录日志。

        副作用:
            把事件写入当前订阅者的内存队列。
        """

        if event.turn_id is None:
            return
        with self._lock:
            published_ids = self._published_event_ids_by_turn.setdefault(event.turn_id, set())
            if event.event_id in published_ids:
                return
            published_ids.add(event.event_id)
            queues = list(self._subscribers.get(event.turn_id, set()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(event)
                log.warning(
                    "runtime_event_subscriber_queue_full",
                    extra={
                        "msg": "runtime event subscriber queue full; dropped oldest event",
                        "data": {"turn_id": event.turn_id},
                    },
                )

    def close_turn(self, turn_id: str) -> None:
        """Close all current subscriptions for a turn.

        参数:
            turn_id: 需要关闭订阅的 turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            向当前订阅队列写入关闭哨兵。
        """

        with self._lock:
            queues = list(self._subscribers.pop(turn_id, set()))
            self._published_event_ids_by_turn.pop(turn_id, None)
        for queue in queues:
            try:
                queue.put_nowait(_QUEUE_CLOSED)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(_QUEUE_CLOSED)
