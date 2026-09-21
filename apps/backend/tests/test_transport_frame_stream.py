from __future__ import annotations

import asyncio

import pytest

from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber


def mutation_frame(value: str, *, path: tuple[str | int, ...] = ("runs", 0, "messages", 0, "parts", 0, "text")) -> TransportFrame:
    return TransportFrame(
        task_id=1,
        kind="mutation",
        mutations=(ConversationStateMutation("append-text", path, value),),
        source_run_id=1,
        current_run_id=1,
        current_run_status="running",
    )


@pytest.mark.asyncio
async def test_subscriber_coalesces_adjacent_text_and_prioritizes_control_frame() -> None:
    subscriber = Subscriber(asyncio.get_running_loop(), mutation_limit=4)
    subscriber.offer(mutation_frame("a"))
    subscriber.offer(mutation_frame("b"))
    subscriber.offer(
        TransportFrame(
            task_id=1,
            kind="resync_required",
            mutations=(),
            resync_reason="subscriber_backpressure",
        )
    )

    control = await subscriber.get()
    assert control.kind == "resync_required"
    # A connection-level recovery/full boundary invalidates queued mutations; the client must
    # attach again instead of applying frames produced before the boundary.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscriber.get(), 0.01)


@pytest.mark.asyncio
async def test_subscriber_overflow_emits_resync_without_unbounded_growth() -> None:
    subscriber = Subscriber(asyncio.get_running_loop(), mutation_limit=1)
    subscriber.offer(mutation_frame("a", path=("runs", 0, "messages", 0, "parts", 0, "text")))
    subscriber.offer(mutation_frame("b", path=("runs", 0, "messages", 1, "parts", 0, "text")))
    subscriber.offer(mutation_frame("c", path=("runs", 0, "messages", 2, "parts", 0, "text")))

    frame = await subscriber.get()
    assert frame.kind == "resync_required"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscriber.get(), 0.01)


@pytest.mark.asyncio
async def test_subscriber_does_not_coalesce_across_run_status_boundary() -> None:
    subscriber = Subscriber(asyncio.get_running_loop(), mutation_limit=4)
    subscriber.offer(mutation_frame("a"))
    subscriber.offer(
        TransportFrame(
            task_id=1,
            kind="mutation",
            mutations=(ConversationStateMutation(
                "append-text",
                ("runs", 0, "messages", 0, "parts", 0, "text"),
                "b",
            ),),
            source_run_id=1,
            current_run_id=1,
            current_run_status="completed",
        )
    )

    first = await subscriber.get()
    second = await subscriber.get()
    assert first.mutations[0].value == "a"
    assert second.mutations[0].value == "b"
