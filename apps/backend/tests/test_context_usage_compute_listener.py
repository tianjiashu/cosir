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


class _Projector:
    def __init__(self) -> None:
        self.events: list[object] = []

    def process(self, event: object) -> None:
        self.events.append(event)


def test_context_usage_publishes_event_and_persists_absolute_window(monkeypatch) -> None:
    task_service = _TaskService()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_task_service",
        lambda: task_service,
    )
    projector = _Projector()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_conversation_event_projector",
        lambda: projector,
    )
    listener = ContextUsageComputeListener(task_id=7, run_id=11)

    listener.listen(
        ListenerEvent(
            type=ContextEventType.ADD_MESSAGE,
            entries=[ContextEntry(HumanMessage(content="hello"), 11, 0)],
            usage=0,
            total_tokens=100,
        ),
        ListenerResult(usage=0),
    )

    assert len(projector.events) == 1
    assert projector.events[0].type == "context_usage_updated"
    assert projector.events[0].task_id == 7
    assert projector.events[0].run_id == 11
    assert projector.events[0].used_tokens == 1
    assert projector.events[0].context_window_tokens == 100
    assert task_service.updates == [(7, 1)]


def test_context_usage_task_write_is_failure_safe(monkeypatch) -> None:
    task_service = _TaskService()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_task_service",
        lambda: task_service,
    )
    projector = _Projector()
    monkeypatch.setattr(
        "app.core.context.context_listener.context_usage_compute_listener.get_conversation_event_projector",
        lambda: projector,
    )
    def fail_update(_task_id: int, _used: int) -> None:
        raise RuntimeError("database unavailable")

    task_service.update_context_usage = fail_update  # type: ignore[method-assign]
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

    assert len(projector.events) == 1
    assert projector.events[0].run_id is None
