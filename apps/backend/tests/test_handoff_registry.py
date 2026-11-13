"""交接登记表：同一资源至多一次交接、各结束路径恰好结束一次、变化通知在锁外。"""

from __future__ import annotations

import threading

import pytest

from app.core.tool.handoff.handoff_registry import (
    HandoffConflictError,
    HandoffLease,
    HandoffRegistry,
)


def _lease(call_id: str = "c1", resource: str = "t1", session_id: int = 1) -> HandoffLease:
    return HandoffLease(
        tool_call_id=call_id,
        session_id=session_id,
        tool_name="browser_handoff",
        resource_id=resource,
        reason="sign in",
    )


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
    assert [lease for lease in registry.leases(1) if lease.resource_id == "t1"] == []


def test_drop_only_touches_the_given_session_and_calls() -> None:
    registry = HandoffRegistry()
    registry.begin(_lease("c1", "t1", session_id=1))
    registry.begin(_lease("c2", "t2", session_id=2))
    registry.drop(1, ["c2", "c1", "missing"])
    assert registry.leases(1) == []
    assert [lease.tool_call_id for lease in registry.leases(2)] == ["c2"]
    registry.drop_session(2)
    assert registry.leases(2) == []


def test_changes_notify_the_listener_off_the_callers_thread() -> None:
    registry = HandoffRegistry()
    seen: list[tuple[int, str]] = []
    done = threading.Event()

    def listener(session_id: int) -> None:
        seen.append((session_id, threading.current_thread().name))
        if len(seen) == 2:
            done.set()

    registry.set_listener(listener)
    registry.begin(_lease())
    registry.end(1, "c1")
    assert done.wait(2)
    assert [session_id for session_id, _ in seen] == [1, 1]
    assert all(name.startswith("handoff-notify") for _, name in seen)
