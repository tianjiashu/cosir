from __future__ import annotations

import asyncio

import pytest

from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber

_TEXT_PATH = ("messages", 0, "parts", 0, "text")


def mutation_frame(value: str, *, path: tuple[str | int, ...] = _TEXT_PATH) -> TransportFrame:
    return TransportFrame(
        task_id=1,
        kind="mutation",
        mutations=(ConversationStateMutation("append-text", path, value),),
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
    subscriber.offer(mutation_frame("a", path=("messages", 0, "parts", 0, "text")))
    subscriber.offer(mutation_frame("b", path=("messages", 1, "parts", 0, "text")))
    subscriber.offer(mutation_frame("c", path=("messages", 2, "parts", 0, "text")))

    frame = await subscriber.get()
    assert frame.kind == "resync_required"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscriber.get(), 0.01)


@pytest.mark.asyncio
async def test_subscriber_coalesces_adjacent_text_on_same_path() -> None:
    subscriber = Subscriber(asyncio.get_running_loop(), mutation_limit=4)
    subscriber.offer(mutation_frame("a"))
    subscriber.offer(mutation_frame("b"))

    frame = await subscriber.get()
    assert [mutation.value for mutation in frame.mutations] == ["ab"]


@pytest.mark.asyncio
async def test_subscriber_does_not_coalesce_across_activity_change() -> None:
    """帧不再携带 Run 状态；活动变化本身是一条 mutation，夹在中间时前后文本不能合并。"""

    subscriber = Subscriber(asyncio.get_running_loop(), mutation_limit=4)
    subscriber.offer(mutation_frame("a"))
    subscriber.offer(
        TransportFrame(
            task_id=1,
            kind="mutation",
            mutations=(ConversationStateMutation("set", ("activity", "status"), "completed"),),
        )
    )
    subscriber.offer(mutation_frame("b"))

    first = await subscriber.get()
    second = await subscriber.get()
    third = await subscriber.get()
    assert first.mutations[0].value == "a"
    assert second.mutations[0].path == ("activity", "status")
    assert third.mutations[0].value == "b"
