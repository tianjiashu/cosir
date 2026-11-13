"""浏览器标签回收对账：链路连上时补发丢掉的 Task 删除通知。"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from typing import Any

import pytest

from app.service import depends as service_depends
from app.service.browser.tab_reclaim import reclaim_orphan_tabs
from app.tools.host.errors import UNAVAILABLE, HostCallError


class _Link:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[str] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    def submit(self, method: str, params: dict[str, Any], meta: dict[str, Any]) -> Future[Any]:
        self.calls.append(method)
        future: Future[Any] = Future()
        if isinstance(self.outcome, BaseException):
            future.set_exception(self.outcome)
        else:
            future.set_result(self.outcome)
        return future

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


class _Tasks:
    def __init__(self, existing: set[int]) -> None:
        self.existing = existing
        self.asked: list[set[int]] = []

    def existing_task_ids(self, task_ids: set[int]) -> set[int]:
        self.asked.append(task_ids)
        return task_ids & self.existing


def _install(
    monkeypatch: pytest.MonkeyPatch, outcome: object, existing: set[int]
) -> tuple[_Link, _Tasks]:
    link, tasks = _Link(outcome), _Tasks(existing)
    monkeypatch.setattr(service_depends, "get_host_link", lambda: link)
    monkeypatch.setattr(service_depends, "get_task_service", lambda: tasks)
    return link, tasks


def test_drops_tasks_the_host_still_holds_but_the_store_no_longer_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link, tasks = _install(monkeypatch, {"task_ids": [2, 3, 5]}, existing={3, 9})

    asyncio.run(reclaim_orphan_tabs())

    assert link.calls == ["browser.taskIds"]
    assert tasks.asked == [{2, 3, 5}]
    assert link.notifications == [("browser.dropTasks", {"task_ids": [2, 5]})]


def test_sends_nothing_when_every_held_task_still_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    link, _ = _install(monkeypatch, {"task_ids": [3]}, existing={3})

    asyncio.run(reclaim_orphan_tabs())

    assert link.notifications == []


def test_skips_the_store_when_the_host_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    link, tasks = _install(monkeypatch, {"task_ids": []}, existing=set())

    asyncio.run(reclaim_orphan_tabs())

    assert tasks.asked == []
    assert link.notifications == []


def test_a_host_that_went_away_again_is_left_for_the_next_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link, tasks = _install(monkeypatch, HostCallError(UNAVAILABLE, "gone"), existing=set())

    asyncio.run(reclaim_orphan_tabs())

    assert tasks.asked == []
    assert link.notifications == []
