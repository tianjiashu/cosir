"""Task 执行状态推送：只在有订阅者时读库，同一 Task 只留最新值，流先发整表。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.task_status_api import task_status_stream
from app.service.task_status.task_status_feed import TaskStatusFeed


class _Reads:
    def __init__(self, statuses: dict[int, str | None]) -> None:
        self.statuses = statuses
        self.reads = 0

    def status(self, task_id: int) -> str | None:
        self.reads += 1
        return self.statuses[task_id]

    def list_statuses(self) -> dict[int, str | None]:
        return dict(self.statuses)


def _feed(statuses: dict[int, str | None]) -> tuple[TaskStatusFeed, _Reads]:
    feed = TaskStatusFeed()
    reads = _Reads(statuses)
    feed._task_reads = reads  # type: ignore[assignment]
    return feed, reads


def test_publish_without_subscribers_does_not_read() -> None:
    feed, reads = _feed({1: "running"})
    feed.publish(1)
    assert reads.reads == 0


@pytest.mark.asyncio
async def test_subscribers_get_the_latest_status_per_task() -> None:
    feed, reads = _feed({1: "running", 2: None})
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish(1)
    reads.statuses[1] = "completed"
    feed.publish(1)
    feed.publish(2)
    feed.publish(99)  # 已删除的 Task 不发
    await asyncio.sleep(0)

    assert subscription.changed.is_set()
    assert subscription.take() == {1: "completed", 2: None}
    assert not subscription.changed.is_set()

    feed.unsubscribe(subscription)
    feed.publish(1)
    await asyncio.sleep(0)
    assert subscription.take() == {}


@pytest.mark.asyncio
async def test_stream_starts_with_the_full_table_then_sends_changes() -> None:
    feed, reads = _feed({1: "running", 2: "completed"})
    response = await task_status_stream(feed)
    body = response.body_iterator

    first = json.loads(await anext(body))
    assert first == {"type": "full", "statuses": {"1": "running", "2": "completed"}}

    reads.statuses[1] = "suspended"
    feed.publish(1)
    second = json.loads(await anext(body))
    assert second == {"type": "changed", "statuses": {"1": "suspended"}}
    await body.aclose()
    assert not feed._subscriptions
