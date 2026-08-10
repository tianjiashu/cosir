"""Subscribe-only turn event stream tests."""

import pytest
from fastapi import HTTPException

from app.api.turns_api import subscribe_turn_events
from app.models import TurnRecord
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.utils.datetime_utils import utc_now


class _TurnService:
    """Provide a minimal real turn lookup boundary for endpoint tests."""

    def __init__(self, turn: TurnRecord | None) -> None:
        """Store the lookup result returned by ``get_turn``."""

        self._turn = turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        """Return the configured turn or model the production missing-turn error."""

        if self._turn is None:
            raise KeyError(turn_id)
        return self._turn


class _ClosingEventBus(RuntimeEventBus):
    """Close the subscription immediately so the test can consume its response."""

    def subscribe(self, turn_id: str):
        """Subscribe through the real bus, then close the test stream."""

        subscription = super().subscribe(turn_id)
        self.close_turn(turn_id)
        return subscription

    def claim_turn_producer(self, turn_id: str) -> bool:
        """Raise if the subscribe-only endpoint attempts to become a producer."""

        raise AssertionError(f"subscribe-only endpoint claimed producer for {turn_id}")


def _turn(status: str) -> TurnRecord:
    """Build a persisted turn-shaped value for endpoint behavior tests."""

    now = utc_now()
    return TurnRecord(
        turn_id="turn_child",
        task_id="task_1",
        input_text="review this",
        status=status,
        created_at=now,
        updated_at=now,
        parent_turn_id="turn_parent",
        delegation_id="del_1",
    )


async def test_subscribe_turn_events_rejects_missing_turn():
    """Return 404 when the requested child turn does not exist."""

    with pytest.raises(HTTPException) as exc_info:
        await subscribe_turn_events("missing", _TurnService(None), RuntimeEventBus())

    assert exc_info.value.status_code == 404


async def test_subscribe_turn_events_rejects_terminal_turn():
    """Return 409 when the requested child turn is already terminal."""

    with pytest.raises(HTTPException) as exc_info:
        await subscribe_turn_events(
            "turn_child", _TurnService(_turn("completed")), RuntimeEventBus()
        )

    assert exc_info.value.status_code == 409


async def test_subscribe_turn_events_returns_sse_without_invoking_runtime():
    """Subscribe to a pending child turn without claiming or running its producer."""

    response = await subscribe_turn_events(
        "turn_child", _TurnService(_turn("pending")), _ClosingEventBus()
    )

    assert response.media_type.startswith("text/event-stream")
    assert [chunk async for chunk in response.body_iterator] == []
