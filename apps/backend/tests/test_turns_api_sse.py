"""针对 app.api.turns_api._sse_turn_events 的断连兜底行为测试。

覆盖改动点：try/finally 中，当客户端断开且 turn 仍 running 时，调用
turn_service.update_turn_status(turn_id, 'failed', end_reason='client_disconnected')。

导入前置：仓库存在坏导入链，需在导入 turns_api 之前注入 app.storage.crud.log 占位。
"""

import sys

sys.path.insert(0, ".")

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

import pytest

from app.api import turns_api
from app.models.runtime_event import RuntimeEvent
from app.models.enums.event_type import EventType


# ---------------------------------------------------------------------------
# Fake runtime / turn_service
# ---------------------------------------------------------------------------


class FakeTurnService:
    """记录 has_turn_status / update_turn_status 调用，可注入状态。"""

    def __init__(self, status="running"):
        self._status = status
        self.update_calls: list[tuple] = []
        self.has_status_calls: list[tuple] = []

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        self.has_status_calls.append((turn_id, status))
        return self._status == status

    def update_turn_status(self, turn_id: str, status: str, end_reason: str | None = None):
        self.update_calls.append((turn_id, status, end_reason))
        self._status = status
        return None


class FakeRuntimeNormal:
    """run_turn 正常产出若干事件后正常结束。"""

    def __init__(self, events):
        self._events = events
        self.run_turn_called: list[tuple] = []

    async def run_turn(self, turn_id, turn=None):
        self.run_turn_called.append((turn_id, turn))
        for ev in self._events:
            yield ev


class FakeRuntimeRaise:
    """run_turn 产出一条事件后抛出异常，模拟客户端断开/异常中止。"""

    def __init__(self, events, exc):
        self._events = events
        self._exc = exc

    async def run_turn(self, turn_id, turn=None):
        for ev in self._events:
            yield ev
        raise self._exc


class FakeRuntimeRaiseBeforeFirst:
    """run_turn 在产出任何事件前就抛异常。"""

    def __init__(self, exc):
        self._exc = exc

    async def run_turn(self, turn_id, turn=None):
        raise self._exc
        yield  # pragma: no cover


async def collect_sse(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


# 测试目的：正常事件流结束后，turn 已非 running，不应调用 update_turn_status。缺陷：误标记终态。
async def test_sse_normal_flow_no_status_update():
    events = [
        RuntimeEvent(event_type=EventType.RUN_STARTED, task_id="task1", turn_id="t1", payload={}),
        RuntimeEvent(event_type=EventType.RUN_FINISHED, task_id="task1", turn_id="t1", payload={"status": "completed"}),
    ]
    runtime = FakeRuntimeNormal(events)
    # turn 已非 running（例如 completed）
    svc = FakeTurnService(status="completed")
    out = await collect_sse(turns_api._sse_turn_events(runtime, "t1", svc, None))
    assert len(out) == 2
    assert out[0].startswith("event: run_started")
    assert "client_disconnected" not in out[1]
    assert svc.update_calls == []


# 测试目的：run_turn 抛异常（客户端断开）且 turn 仍 running 时，finally 应把 turn 标记为 failed/client_disconnected。缺陷：断连后孤儿 running 未收敛。
async def test_sse_disconnect_while_running_marks_failed():
    events = [
        RuntimeEvent(event_type=EventType.RUN_STARTED, task_id="task1", turn_id="t1", payload={}),
    ]
    runtime = FakeRuntimeRaise(events, RuntimeError("client disconnected"))
    svc = FakeTurnService(status="running")
    with pytest.raises(RuntimeError):
        await collect_sse(turns_api._sse_turn_events(runtime, "t1", svc, None))
    assert svc.update_calls == [("t1", "failed", "client_disconnected")]


# 测试目的：run_turn 在首事件前即抛异常且 turn 仍 running，finally 仍应兜底标记 failed。缺陷：首事件前断连未兜底。
async def test_sse_disconnect_before_first_event_marks_failed():
    runtime = FakeRuntimeRaiseBeforeFirst(RuntimeError("boom"))
    svc = FakeTurnService(status="running")
    with pytest.raises(RuntimeError):
        await collect_sse(turns_api._sse_turn_events(runtime, "t1", svc, None))
    assert svc.update_calls == [("t1", "failed", "client_disconnected")]


# 测试目的：run_turn 抛异常但 turn 已非 running（被其它路径处理），finally 不应重复更新。缺陷：重复/错误更新终态。
async def test_sse_disconnect_but_turn_already_non_running_no_update():
    events = [
        RuntimeEvent(event_type=EventType.RUN_STARTED, task_id="task1", turn_id="t1", payload={}),
    ]
    runtime = FakeRuntimeRaise(events, RuntimeError("hmm"))
    svc = FakeTurnService(status="failed")
    with pytest.raises(RuntimeError):
        await collect_sse(turns_api._sse_turn_events(runtime, "t1", svc, None))
    assert svc.update_calls == []


# 测试目的：断连兜底更新必须使用 end_reason='client_disconnected'（精确断言），而非其它字符串。缺陷：end_reason 拼写/取值错误。
async def test_sse_disconnect_end_reason_exact():
    events = [RuntimeEvent(event_type=EventType.RUN_STARTED, task_id="task1", turn_id="t1", payload={})]
    runtime = FakeRuntimeRaise(events, Exception("x"))
    svc = FakeTurnService(status="running")
    with pytest.raises(Exception):
        await collect_sse(turns_api._sse_turn_events(runtime, "t1", svc, None))
    assert len(svc.update_calls) == 1
    _tid, _status, end_reason = svc.update_calls[0]
    assert _status == "failed"
    assert end_reason == "client_disconnected"
