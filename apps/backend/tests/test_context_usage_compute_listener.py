from langchain_core.messages import HumanMessage

from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult


class _TaskService:
    def __init__(self) -> None:
        self.updates: list[tuple[int, int]] = []

    def update_context_usage(self, task_id: int, used: int) -> None:
        self.updates.append((task_id, used))


def test_context_usage_publishes_event_through_explicit_sink(monkeypatch) -> None:
    task_service = _TaskService()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_task_service",
        lambda: task_service,
    )
    events: list[object] = []
    listener = ContextUsageComputeListener(
        task_id=7,
        run_id=11,
        publish_event=events.append,
    )

    listener.listen(
        ListenerEvent(
            type=ContextEventType.ADD_MESSAGE,
            entries=[ContextEntry(HumanMessage(content="hello"), 11)],
            usage=0,
            total_tokens=100,
        ),
        ListenerResult(usage=0),
    )

    assert len(events) == 1
    assert events[0].type == "context_usage_updated"
    assert events[0].task_id == 7
    assert events[0].run_id == 11
    assert task_service.updates == [(7, 1)]


def test_context_usage_does_not_emit_without_run_sink(monkeypatch) -> None:
    task_service = _TaskService()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_task_service",
        lambda: task_service,
    )
    events: list[object] = []
    listener = ContextUsageComputeListener(task_id=7)

    listener.listen(
        ListenerEvent(
            type=ContextEventType.LOAD_HISTORY,
            entries=[],
            usage=0,
            total_tokens=100,
        ),
        ListenerResult(usage=0),
    )

    assert events == []
    assert task_service.updates == [(7, 0)]
