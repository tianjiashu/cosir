"""Runtime event persistence plus in-process broadcast."""

from __future__ import annotations

import dataclasses

from app.models.event.runtime_event import RuntimeEvent
from app.service import depends as service_depends
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus


class RuntimeEventService:
    """Persist runtime events and publish the saved event to live subscribers."""

    def __init__(self) -> None:
        """Initialize the runtime event service.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            从 service 依赖入口取得 CRUD 与 bus 单例并保存引用。
        """

        self._runtime_event_crud = service_depends.get_runtime_event_crud()
        self._event_bus = service_depends.get_runtime_event_bus()

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
        """持久化一个事件并向实时订阅者发布持久化后的版本。

        职责边界（与 RuntimeEventBus 对齐）：
        - 落库：无论 ``turn_id`` 是否为 None 都会写入 ``runtime_events`` 表，
          供 ``list_by_task`` / ``list_by_turn`` 历史回看（含无 turn 归属的全局事件）。
        - 实时投递：仅 ``turn_id`` 非 None 的事件会被 RuntimeEventBus 投递给已订阅
          该 turn 的消费者；``turn_id`` 为 None 的事件无实时订阅通道，bus 会按设计
          静默跳过，不表示丢事件。

        参数:
            event: 待保存并发布的 runtime event。

        返回:
            已写入真实 sequence 的 RuntimeEvent。

        异常:
            RuntimeError: 当 runtime event 持久化失败时抛出。

        副作用:
            向 runtime_events 表写入事件；仅当事件归属某 turn 时向进程内订阅者发布。
        """

        stamped = self.save_event(event)
        self.publish_event(stamped)
        return stamped

    def list_by_task(self, task_id: int) -> list[dict[str, object]]:
        """List persisted runtime events under a task.

        参数:
            task_id: 任务标识。

        返回:
            按 turn 与 sequence 升序排列的事件字典列表。

        异常:
            RuntimeError: 如果底层查询失败。

        副作用:
            读取 runtime_events 表。
        """

        return self._runtime_event_crud.list_by_task(task_id)

    def list_by_turn(self, turn_id: int) -> list[dict[str, object]]:
        """List persisted runtime events under a turn.

        参数:
            turn_id: 轮次标识。

        返回:
            按 sequence 升序排列的事件字典列表。

        异常:
            RuntimeError: 如果底层查询失败。

        副作用:
            读取 runtime_events 表。
        """

        return self._runtime_event_crud.list_by_turn(turn_id)
