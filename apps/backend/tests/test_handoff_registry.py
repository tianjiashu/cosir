"""交接登记表：同一资源至多一次交接、各结束路径恰好结束一次、变化通知在锁外。"""

from __future__ import annotations

import threading

import pytest

from app.core.tool.handoff.handoff_registry import (
    HandoffConflictError,
    HandoffLease,
    HandoffRegistry,
)
from app.service import depends


def _lease(call_id: str = "c1", resource: str = "t1", task_id: int = 1) -> HandoffLease:
    return HandoffLease(
        tool_call_id=call_id,
        task_id=task_id,
        tool_name="browser_handoff",
        surface="browser",
        resource_id=resource,
        reason="sign in",
    )


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, str, str | None]]:
    calls: list[tuple[int, str, str, str | None]] = []

    class _Decisions:
        def submit(
            self, task_id, tool_call_id, decision, *, feedback=None, data=None, source="user"
        ):
            calls.append((task_id, tool_call_id, decision, feedback))

    monkeypatch.setattr(depends, "get_tool_decision_service", lambda: _Decisions())
    return calls


def test_one_handoff_per_resource_and_end_returns_the_lease_once() -> None:
    registry = HandoffRegistry()
    registry.begin(_lease())
    with pytest.raises(HandoffConflictError) as conflict:
        registry.begin(_lease("c2"))
    assert conflict.value.existing.tool_call_id == "c1"
    # 另一个资源不受影响。
    registry.begin(_lease("c3", resource="t2"))
    assert [lease.tool_call_id for lease in registry.leases(1)] == ["c1", "c3"]

    assert registry.end(1, "c1") == _lease()
    assert registry.end(1, "c1") is None
    assert registry.on_resource("browser", "t1") is None


def test_settle_rejects_on_the_users_behalf_once(decisions) -> None:
    registry = HandoffRegistry()
    registry.begin(_lease())
    registry.settle(1, "c1", "the tab was closed")
    registry.settle(1, "c1", "the tab was closed")
    assert decisions == [(1, "c1", "reject", "the tab was closed")]
    assert registry.leases(1) == []


def test_drop_only_touches_the_given_task_and_calls(decisions) -> None:
    registry = HandoffRegistry()
    registry.begin(_lease("c1", "t1", task_id=1))
    registry.begin(_lease("c2", "t2", task_id=2))
    registry.drop(1, ["c2", "c1", "missing"])
    assert registry.leases(1) == []
    assert [lease.tool_call_id for lease in registry.leases(2)] == ["c2"]
    registry.drop_task(2)
    assert registry.leases(2) == []
    assert decisions == []


def test_changes_notify_the_listener_off_the_callers_thread() -> None:
    registry = HandoffRegistry()
    seen: list[tuple[int, str]] = []
    done = threading.Event()

    def listener(task_id: int) -> None:
        seen.append((task_id, threading.current_thread().name))
        if len(seen) == 2:
            done.set()

    registry.set_listener(listener)
    registry.begin(_lease())
    registry.end(1, "c1")
    assert done.wait(2)
    assert [task_id for task_id, _ in seen] == [1, 1]
    assert all(name.startswith("handoff-notify") for _, name in seen)
