"""Session 推送：状态只在有订阅者时读库、同一 Session 只留最新值；目录失效按工作区去重、只收侧栏 Session；
流先发整表，再分别发状态变化与目录失效。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.session_feed_api import session_feed_stream
from app.models.session_kind import SessionType
from app.service.session_feed.session_feed import CatalogChange, SessionFeed
from app.storage.queries.session_status_queries import SessionStatus


class _Reads:
    def __init__(self, statuses: dict[int, SessionStatus]) -> None:
        self.statuses = statuses
        self.reads = 0

    def status(self, session_id: int) -> SessionStatus:
        self.reads += 1
        return self.statuses[session_id]

    def list_statuses(self) -> dict[int, SessionStatus]:
        return dict(self.statuses)


class _Session:
    def __init__(self, workspace_id: int, session_type: SessionType) -> None:
        self.workspace_id = workspace_id
        self.session_type = session_type


class _Sessions:
    def __init__(self, sessions: dict[int, _Session]) -> None:
        self.sessions = sessions
        self.reads = 0

    def get(self, session_id: int) -> _Session:
        self.reads += 1
        return self.sessions[session_id]


def _feed(
    statuses: dict[int, SessionStatus], sessions: dict[int, _Session] | None = None
) -> tuple[SessionFeed, _Reads, _Sessions]:
    feed = SessionFeed()
    reads = _Reads(statuses)
    session_reads = _Sessions(sessions or {})
    feed._session_reads = reads  # type: ignore[assignment]
    feed._session_crud = session_reads  # type: ignore[assignment]
    return feed, reads, session_reads


def test_publish_without_subscribers_does_not_read() -> None:
    feed, reads, sessions = _feed({1: SessionStatus("running", 0)}, {1: _Session(7, SessionType.USER)})
    feed.publish_status(1)
    feed.publish_session_catalog(1)
    assert reads.reads == 0
    assert sessions.reads == 0


@pytest.mark.asyncio
async def test_subscribers_get_the_latest_status_per_session() -> None:
    feed, reads, _ = _feed({1: SessionStatus("running", 0), 2: SessionStatus(None, 0)})
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish_status(1)
    reads.statuses[1] = SessionStatus("completed", 1)
    feed.publish_status(1)
    feed.publish_status(2)
    feed.publish_status(99)  # 已删除的 Session 不发
    await asyncio.sleep(0)

    assert subscription.changed.is_set()
    batch = subscription.take()
    assert batch.statuses == {1: SessionStatus("completed", 1), 2: SessionStatus(None, 0)}
    assert not batch.catalog
    assert not subscription.changed.is_set()

    feed.unsubscribe(subscription)
    feed.publish_status(1)
    await asyncio.sleep(0)
    assert subscription.take().statuses == {}


@pytest.mark.asyncio
async def test_catalog_changes_only_cover_sidebar_sessions_and_merge_per_workspace() -> None:
    feed, _, _ = _feed(
        {},
        {1: _Session(7, SessionType.USER), 2: _Session(7, SessionType.DELEGATED), 3: _Session(8, SessionType.AGENT_FORK)},
    )
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish_session_catalog(1)
    feed.publish_session_catalog(2)  # 委派子 Agent 不在侧栏
    feed.publish_session_catalog(3)
    feed.publish_session_catalog(99)  # 已删除：删除方自己发过
    feed.publish_catalog(7, SessionType.USER_FORK)
    feed.publish_catalog(9, SessionType.DELEGATED)
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
    feed, reads, _ = _feed({1: SessionStatus("running", 0), 2: SessionStatus("completed", 1)})
    response = await session_feed_stream(feed)
    body = response.body_iterator

    first = json.loads(await anext(body))
    assert first == {
        "type": "full",
        "sessions": {
            "1": {"executionStatus": "running", "awaitingUserCalls": 0},
            "2": {"executionStatus": "completed", "awaitingUserCalls": 1},
        },
    }

    reads.statuses[1] = SessionStatus("suspended", 1)
    feed.publish_status(1)
    second = json.loads(await anext(body))
    assert second == {
        "type": "changed",
        "sessions": {"1": {"executionStatus": "suspended", "awaitingUserCalls": 1}},
    }

    feed.publish_catalog(4, SessionType.USER)
    feed.publish_catalog(3, SessionType.USER)
    third = json.loads(await anext(body))
    assert third == {"type": "catalog", "workspaces": False, "workspaceIds": [3, 4]}
    await body.aclose()
    assert not feed._subscriptions
