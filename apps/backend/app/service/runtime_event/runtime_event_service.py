"""Runtime event persistence plus in-process broadcast."""

from __future__ import annotations

import dataclasses

from app.models.runtime_event import RuntimeEvent
from app.service.runtime_event.runtime_event_bus import RuntimeEventBus
from app.storage.crud.runtime_event_crud import RuntimeEventCrud


class RuntimeEventService:
    """Persist runtime events and publish the saved event to live subscribers."""

    def __init__(self, runtime_event_crud: RuntimeEventCrud, event_bus: RuntimeEventBus) -> None:
        """Initialize the runtime event service.

        参数:
            runtime_event_crud: runtime_events 表 CRUD。
            event_bus: 进程内 runtime event 广播总线。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 CRUD 与 bus 引用。
        """

        self._runtime_event_crud = runtime_event_crud
        self._event_bus = event_bus

    @property
    def event_bus(self) -> RuntimeEventBus:
        """Return the underlying event bus.

        参数:
            无。

        返回:
            RuntimeEventBus。

        异常:
            无。

        副作用:
            无。
        """

        return self._event_bus

    def save_event(self, event: RuntimeEvent) -> RuntimeEvent:
        """Persist one runtime event with the next turn-local sequence.

        参数:
            event: 待持久化的运行时事件。

        返回:
            已写入真实 sequence 的 RuntimeEvent。

        异常:
            RuntimeError: 当 runtime event 持久化失败时抛出。

        副作用:
            向 runtime_events 表写入一条事件。
        """

        if event.turn_id is None:
            self._runtime_event_crud.save_event(event.to_dict())
            return event
        sequence = self._runtime_event_crud.save_event_with_next_sequence(event.to_dict())
        return dataclasses.replace(event, sequence=sequence)

    def publish_event(self, event: RuntimeEvent) -> None:
        """Publish an event to live subscribers.

        参数:
            event: 待发布的 runtime event。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 RuntimeEventBus 订阅队列。
        """

        self._event_bus.publish(event)

    def save_and_publish(self, event: RuntimeEvent) -> RuntimeEvent:
        """Persist one event and publish the persisted version.

        参数:
            event: 待保存并发布的 runtime event。

        返回:
            已写入真实 sequence 的 RuntimeEvent。

        异常:
            RuntimeError: 当 runtime event 持久化失败时抛出。

        副作用:
            向 runtime_events 表写入事件，并向当前进程内订阅者发布事件。
        """

        stamped = self.save_event(event)
        self.publish_event(stamped)
        return stamped
