"""Session 推送：状态只在有订阅者时读库、同一 Session 只留最新值，终端批准单独计在显示它的会话上；
目录失效按工作区去重、只收侧栏 Session；流先发整表，再分别发状态变化与目录失效。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.api.session_feed_api import session_feed_stream
from app.core.tool.terminal_approval.registry import TerminalApprovalRegistry
from app.models.session.session_kind import SessionType
from app.models.session.session_status import SessionStatus
from app.service.session import session_feed as session_feed_module
from app.service.session.session_feed import CatalogChange, SessionFeed


class _Session:
    def __init__(self, workspace_id: int, session_type: SessionType) -> None:
        self.workspace_id = workspace_id
        self.session_type = session_type


class _Sessions:
    """``SessionCrud`` 的替身：状态读取与记录读取分开计数。"""

    def __init__(self, statuses: dict[int, SessionStatus], sessions: dict[int, _Session]) -> None:
        self.statuses = statuses
        self.sessions = sessions
        self.status_reads = 0
        self.record_reads = 0

    def get_status(self, session_id: int) -> SessionStatus:
        self.status_reads += 1
        return self.statuses[session_id]

    def list_statuses(self) -> dict[int, SessionStatus]:
        return dict(self.statuses)

    def get(self, session_id: int) -> _Session:
        self.record_reads += 1
        return self.sessions[session_id]


def _feed(
    statuses: dict[int, SessionStatus], sessions: dict[int, _Session] | None = None
) -> tuple[SessionFeed, _Sessions]:
    feed = SessionFeed()
    crud = _Sessions(statuses, sessions or {})
    feed._session_crud = crud  # type: ignore[assignment]
    return feed, crud


def test_publish_without_subscribers_does_not_read() -> None:
    feed, sessions = _feed(
        {1: SessionStatus("running", 0, 0, False)}, {1: _Session(7, SessionType.USER)}
    )
    feed.publish_status(1)
    feed.publish_session_catalog(1)
    assert sessions.status_reads == 0
    assert sessions.record_reads == 0


@pytest.mark.asyncio
async def test_subscribers_get_the_latest_status_per_session() -> None:
    feed, sessions = _feed(
        {1: SessionStatus("running", 0, 0, False), 2: SessionStatus(None, 0, 0, False)}
    )
    subscription = feed.subscribe(asyncio.get_running_loop())

    feed.publish_status(1)
    sessions.statuses[1] = SessionStatus("completed", 1, 0, False)
    feed.publish_status(1)
    feed.publish_status(2)
    feed.publish_status(99)  # 已删除的 Session 不发
    await asyncio.sleep(0)

    assert subscription.changed.is_set()
    batch = subscription.take()
    assert batch.statuses == {
        1: SessionStatus("completed", 1, 0, False),
        2: SessionStatus(None, 0, 0, False),
    }
    assert not batch.catalog
    assert not subscription.changed.is_set()

    feed.unsubscribe(subscription)
    feed.publish_status(1)
    await asyncio.sleep(0)
    assert subscription.take().statuses == {}


@pytest.mark.asyncio
async def test_pending_terminal_approvals_count_separately_on_the_display_session(
    monkeypatch,
) -> None:
    registry = TerminalApprovalRegistry()
    monkeypatch.setattr(session_feed_module, "terminal_approvals", registry)
    feed, _ = _feed(
        {1: SessionStatus("running", 1, 0, False), 2: SessionStatus("running", 0, 0, False)}
    )
    # 子 Agent 2 的批准冒泡到用户会话 1：计在显示它的会话上，不计在发起方。
    request = registry.request(  # noqa: S604 - 登记测试描述，不运行 shell
        origin_session_id=2,
        display_session_id=1,
        run_id=5,
        tool_call_id="call-1",
        command="ls",
        shell="zsh",
        cwd=str(Path.cwd()),
        intent="",
        origin_title="测试会话",
    )
    # 批准与 HITL 提问分开计数：会话 1 原有一个提问，现在另有一个批准。
    assert feed.statuses() == {
        1: SessionStatus("running", 1, 1, False),
        2: SessionStatus("running", 0, 0, False),
    }

    subscription = feed.subscribe(asyncio.get_running_loop())
    feed.publish_status(1)
    await asyncio.sleep(0)
    assert subscription.take().statuses == {1: SessionStatus("running", 1, 1, False)}

    registry.cancel(request, "call_cancelled")
    feed.publish_status(1)
    await asyncio.sleep(0)
    assert subscription.take().statuses == {1: SessionStatus("running", 1, 0, False)}
    assert feed.statuses()[1] == SessionStatus("running", 1, 0, False)
    feed.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_catalog_changes_only_cover_sidebar_sessions_and_merge_per_workspace() -> None:
    feed, _ = _feed(
        {},
        {
            1: _Session(7, SessionType.USER),
            2: _Session(7, SessionType.DELEGATED),
            3: _Session(8, SessionType.AGENT_FORK),
        },
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
    feed, sessions = _feed(
        {1: SessionStatus("running", 0, 0, False), 2: SessionStatus("completed", 1, 0, True)}
    )
    response = await session_feed_stream(feed)
    body = response.body_iterator

    first = json.loads(await anext(body))
    assert first == {
        "type": "full",
        "sessions": {
            "1": {
                "executionStatus": "running",
                "awaitingDecisions": 0,
                "awaitingApprovals": 0,
                "unread": False,
            },
            "2": {
                "executionStatus": "completed",
                "awaitingDecisions": 1,
                "awaitingApprovals": 0,
                "unread": True,
            },
        },
    }

    sessions.statuses[1] = SessionStatus("suspended", 1, 0, False)
    feed.publish_status(1)
    second = json.loads(await anext(body))
    assert second == {
        "type": "changed",
        "sessions": {
            "1": {
                "executionStatus": "suspended",
                "awaitingDecisions": 1,
                "awaitingApprovals": 0,
                "unread": False,
            }
        },
    }

    feed.publish_catalog(4, SessionType.USER)
    feed.publish_catalog(3, SessionType.USER)
    third = json.loads(await anext(body))
    assert third == {"type": "catalog", "workspaces": False, "workspaceIds": [3, 4]}
    await body.aclose()
    assert not feed._subscriptions
