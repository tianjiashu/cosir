"""Task 推送：状态只在有订阅者时读库、同一 Task 只留最新值；目录失效按工作区去重、只收侧栏 Task；
流先发整表，再分别发状态变化与目录失效。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.task_feed_api import task_feed_stream
from app.models.task_kind import TaskType
from app.service.task_feed.task_feed import CatalogChange, TaskFeed
from app.storage.queries.task_status_queries import TaskStatus


class _Reads:
    def __init__(self, statuses: dict[int, TaskStatus]) -> None:
        self.statuses = statuses
        self.reads = 0

    def status(self, task_id: int) -> TaskStatus:
        self.reads += 1
        return self.statuses[task_id]

    def list_statuses(self) -> dict[int, TaskStatus]:
        return dict(self.statuses)


class _Task:
    def __init__(self, workspace_id: int, task_type: TaskType) -> None:
        self.workspace_id = workspace_id
        self.task_type = task_type


class _Tasks:
    def __init__(self, tasks: dict[int, _Task]) -> None:
        self.tasks = tasks
        self.reads = 0

    def get(self, task_id: int) -> _Task:
        self.reads += 1
        return self.tasks[task_id]


def _feed(
    statuses: dict[int, TaskStatus], tasks: dict[int, _Task] | None = None
) -> tuple[TaskFeed, _Reads, _Tasks]:
    feed = TaskFeed()
    reads = _Reads(statuses)
    task_reads = _Tasks(tasks or {})
    feed._task_reads = reads  # type: ignore[assignment]
    feed._task_crud = task_reads  # type: ignore[assignment]
    return feed, reads, task_reads


def test_publish_without_subscribers_does_not_read() -> None:
    feed, reads, tasks = _feed({1: TaskStatus("running", 0)}, {1: _Task(7, TaskType.USER)})
    feed.publish_status(1)
    feed.publish_task_catalog(1)
    assert reads.reads == 0
    assert tasks.reads == 0


@pytest.mark.asyncio
async def test_subscribers_get_the_latest_status_per_task() -> None:
    feed, reads, _ = _feed({1: TaskStatus("running", 0), 2: TaskStatus(None, 0)})
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish_status(1)
    reads.statuses[1] = TaskStatus("completed", 1)
    feed.publish_status(1)
    feed.publish_status(2)
    feed.publish_status(99)  # 已删除的 Task 不发
    await asyncio.sleep(0)

    assert subscription.changed.is_set()
    batch = subscription.take()
    assert batch.statuses == {1: TaskStatus("completed", 1), 2: TaskStatus(None, 0)}
    assert not batch.catalog
    assert not subscription.changed.is_set()

    feed.unsubscribe(subscription)
    feed.publish_status(1)
    await asyncio.sleep(0)
    assert subscription.take().statuses == {}


@pytest.mark.asyncio
async def test_catalog_changes_only_cover_sidebar_tasks_and_merge_per_workspace() -> None:
    feed, _, _ = _feed(
        {},
        {1: _Task(7, TaskType.USER), 2: _Task(7, TaskType.AGENT_TASK), 3: _Task(8, TaskType.AGENT_FORK)},
    )
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish_task_catalog(1)
    feed.publish_task_catalog(2)  # 委派子 Agent 不在侧栏
    feed.publish_task_catalog(3)
    feed.publish_task_catalog(99)  # 已删除：删除方自己发过
    feed.publish_catalog(7, TaskType.USER_FORK)
    feed.publish_catalog(9, TaskType.TEAM_NODE_TASK)
    await asyncio.sleep(0)

    batch = subscription.take()
    assert batch.catalog == CatalogChange(workspace_ids=frozenset({7, 8}))
    assert batch.statuses == {}

    feed.publish_workspaces()
    await asyncio.sleep(0)
    assert subscription.take().catalog == CatalogChange(workspaces=True)
    feed.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_stream_starts_with_the_full_table_then_sends_changes() -> None:
    feed, reads, _ = _feed({1: TaskStatus("running", 0), 2: TaskStatus("completed", 1)})
    response = await task_feed_stream(feed)
    body = response.body_iterator

    first = json.loads(await anext(body))
    assert first == {
        "type": "full",
        "tasks": {
            "1": {"executionStatus": "running", "awaitingUserCalls": 0},
            "2": {"executionStatus": "completed", "awaitingUserCalls": 1},
        },
    }

    reads.statuses[1] = TaskStatus("suspended", 1)
    feed.publish_status(1)
    second = json.loads(await anext(body))
    assert second == {
        "type": "changed",
        "tasks": {"1": {"executionStatus": "suspended", "awaitingUserCalls": 1}},
    }

    feed.publish_catalog(4, TaskType.USER)
    feed.publish_catalog(3, TaskType.USER)
    third = json.loads(await anext(body))
    assert third == {"type": "catalog", "workspaces": False, "workspaceIds": [3, 4]}
    await body.aclose()
    assert not feed._subscriptions
