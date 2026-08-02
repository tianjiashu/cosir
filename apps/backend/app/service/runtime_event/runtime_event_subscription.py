import asyncio
from dataclasses import dataclass
from typing import Self

from app.models.runtime_event import RuntimeEvent

# Sentinel pushed into a subscription queue to signal that the subscription
# has been closed. Shared with the event bus via import.
_QUEUE_CLOSED = object()


@dataclass(frozen=True)
class RuntimeEventSubscription:
    """A single turn-scoped runtime event subscription."""

    turn_id: str
    queue: asyncio.Queue[RuntimeEvent | object]

    def __aiter__(self) -> Self:
        """Return this subscription as its own async iterator.

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

    async def __anext__(self) -> RuntimeEvent:
        """Wait for the next runtime event.

        参数:
            无。

        返回:
            下一个 RuntimeEvent。

        异常:
            StopAsyncIteration: 当订阅被关闭时抛出。

        副作用:
            消费订阅队列中的一个元素。
        """

        item = await self.queue.get()
        if item is _QUEUE_CLOSED:
            raise StopAsyncIteration
        if not isinstance(item, RuntimeEvent):
            raise TypeError("runtime event subscription received an invalid item")
        return item
