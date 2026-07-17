"""EventStoreMixin implementation for SQLiteTaskStore."""

from app.storage.crud.task_common import *


class EventStoreMixin:
    """SQLiteTaskStore EventStoreMixin responsibilities."""

    def append_event(self, event: RuntimeEvent) -> None:
        """向任务时间线追加一个事件。

        参数:
            event: 待存储的运行时事件。

        返回:
            无。

        异常:
            KeyError: 如果事件所属任务不存在。

        副作用:
            将事件写入主库。
        """

        self.get_task(event.task_id)
        sequence = event.sequence if event.sequence > 0 else self.next_event_sequence(event.task_id)
        with self._session_factory.begin() as session:
            session.add(
                EventModel(
                    event_id=event.event_id,
                    task_id=event.task_id,
                    turn_id=event.turn_id,
                    sequence=sequence,
                    message_id=event.message_id,
                    tool_call_id=event.tool_call_id,
                    event_type=event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type),
                    payload_json=json.dumps(event.payload),
                    created_at=_to_text(event.created_at),
                )
            )

    def next_event_sequence(self, task_id: str) -> int:
        """返回指定任务下一条事件应使用的稳定序号。

        参数:
            task_id: 需要分配事件序号的任务标识符。

        返回:
            从 1 开始递增的事件序号。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            current = session.execute(
                select(func.max(EventModel.sequence)).where(EventModel.task_id == task_id)
            ).scalar_one()
        return int(current or 0) + 1

    def list_events(self, task_id: str) -> List[RuntimeEvent]:
        """列出一个任务的事件。

        参数:
            task_id: 需返回其事件的任务标识符。

        返回:
            任务事件的有序列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            rows = session.execute(_events_for_task(task_id)).scalars().all()
        return [
            RuntimeEvent(
                event_type=EventType(row.event_type),
                task_id=row.task_id,
                turn_id=row.turn_id,
                sequence=row.sequence,
                message_id=row.message_id,
                tool_call_id=row.tool_call_id,
                payload=json.loads(row.payload_json),
                event_id=row.event_id,
                created_at=_from_text(row.created_at),
            )
            for row in rows
        ]

    def list_events_for_turn(self, turn_id: str) -> List[RuntimeEvent]:
        """列出一个轮次的事件。

        参数:
            turn_id: 需返回其事件的轮次标识符。

        返回:
            轮次事件的有序列表。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            无。
        """

        self.get_turn(turn_id)
        with self._session_factory() as session:
            rows = session.execute(
                select(EventModel)
                .where(EventModel.turn_id == turn_id)
                .order_by(asc(EventModel.sequence), asc(EventModel.created_at), asc(EventModel.event_id))
            ).scalars().all()
        return [
            RuntimeEvent(
                event_type=EventType(row.event_type),
                task_id=row.task_id,
                turn_id=row.turn_id,
                sequence=row.sequence,
                message_id=row.message_id,
                tool_call_id=row.tool_call_id,
                payload=json.loads(row.payload_json),
                event_id=row.event_id,
                created_at=_from_text(row.created_at),
            )
            for row in rows
        ]
