"""Workspace 状态事件订阅对象。

单一职责：封装一次 workspace 状态事件的异步迭代订阅（``async for`` 消费，收到关闭
哨兵即结束）。与 ``RuntimeEventSubscription`` 同构，但按 ``workspace_id`` 关联，供
workspace 级 SSE 端点使用。
"""

import asyncio
from dataclasses import dataclass
from typing import Self

from app.models.event.workspace_event import WorkspaceEvent

# 关闭哨兵：订阅被关闭时写入队列，消费方收到后停止迭代。
_QUEUE_CLOSED = object()


@dataclass(frozen=True)
class WorkspaceEventSubscription:
    """一次 workspace 状态事件订阅。

    参数:
        workspace_id: 订阅的 workspace 标识。
        queue: 进程内事件队列。

    返回:
        一个可异步迭代的订阅对象。

    异常:
        无。

    副作用:
        无。
    """

    workspace_id: int
    queue: asyncio.Queue[WorkspaceEvent | object]

    def __aiter__(self) -> Self:
        """返回自身作为异步迭代器。

        参数:
            无。

        返回:
            当前订阅对象。

        异常:
            无。

        副作用:
            无。
        """

        return self

    async def __anext__(self) -> WorkspaceEvent:
        """等待下一个 workspace 状态事件。

        参数:
            无。

        返回:
            下一个 WorkspaceEvent。

        异常:
            StopAsyncIteration: 当订阅被关闭时抛出。
            TypeError: 当队列中出现非事件对象时抛出。

        副作用:
            消费订阅队列中的一个元素。
        """

        item = await self.queue.get()
        if item is _QUEUE_CLOSED:
            raise StopAsyncIteration
        if not isinstance(item, WorkspaceEvent):
            raise TypeError("workspace event subscription received an invalid item")
        return item
