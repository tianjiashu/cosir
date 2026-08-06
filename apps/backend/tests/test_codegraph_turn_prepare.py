"""CodeGraph 第二阶段方案二 turn 准备编排单元测试（对齐方案二 §六验收 6）。

覆盖：
- TurnWorkspaceResolver.resolve：turn → task → workspace_path 解析；
- TurnPrepareService.prepare_then_execute 各分支：就绪（ready）/ 降级（degraded）/
  跳过（workspace_path=None）；
- 准备事件经 RuntimeEventService.save_and_publish 发布；
- EventType 新增 3 事件已注册 payload（registry 完整性）。

不覆盖：真实 Kernel 进程与索引（归端到端冒烟）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.models.enums.event_type import EventType
from app.models.payload import EVENT_PAYLOAD_MODELS
from app.models.payload.workspace_payload.workspace_degraded_payload import WorkspaceDegradedPayload
from app.models.payload.workspace_payload.workspace_preparing_payload import (
    WorkspacePreparingPayload,
)
from app.models.payload.workspace_payload.workspace_ready_payload import WorkspaceReadyPayload
from app.models.workspace_readiness import WorkspaceReadiness
from app.service.task.turn_prepare_service import TurnPrepareService

# ----------------------------------------------------------------------
# 轻量假对象
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeTurn:
    task_id: str


@dataclass(frozen=True)
class _FakeTask:
    workspace_id: str


@dataclass(frozen=True)
class _FakeWorkspace:
    root_path: str


class _FakeTaskCrud:
    def __init__(self, task: _FakeTask | None) -> None:
        self._task = task

    def get(self, _task_id: str) -> _FakeTask:
        if self._task is None:
            raise KeyError("task not found")
        return self._task


class _FakeWorkspaceCrud:
    def __init__(self, workspace: _FakeWorkspace | None) -> None:
        self._workspace = workspace

    def get(self, _workspace_id: str) -> _FakeWorkspace:
        if self._workspace is None:
            raise KeyError("workspace not found")
        return self._workspace


class _FakeLifecycle:
    def __init__(self, readiness: WorkspaceReadiness) -> None:
        self._readiness = readiness

    def ensure_ready(self, _workspace_path: str) -> WorkspaceReadiness:
        return self._readiness


@dataclass
class _FakeEventService:
    """记录 save_and_publish 调用的事件服务桩。"""

    events: list[Any] = field(default_factory=list)

    def save_and_publish(self, event: Any) -> Any:
        self.events.append(event)
        return event


async def _record_executed(lst: list[bool]) -> None:
    """execute 回调：记录一次执行。"""
    lst.append(True)


# ----------------------------------------------------------------------
# TurnWorkspaceResolver
# ----------------------------------------------------------------------


def test_resolver_resolves_workspace_path():
    from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver

    resolver = TurnWorkspaceResolver.__new__(TurnWorkspaceResolver)
    resolver._task = _FakeTaskCrud(_FakeTask(workspace_id="ws-1"))
    resolver._workspace = _FakeWorkspaceCrud(_FakeWorkspace(root_path="/ws/root"))

    path = resolver.resolve(_FakeTurn(task_id="task-1"))
    assert path == "/ws/root"


def test_resolver_missing_task_raises():
    from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver

    resolver = TurnWorkspaceResolver.__new__(TurnWorkspaceResolver)
    resolver._task = _FakeTaskCrud(None)
    resolver._workspace = _FakeWorkspaceCrud(_FakeWorkspace(root_path="/ws"))

    with pytest.raises(KeyError):
        resolver.resolve(_FakeTurn(task_id="task-x"))


def test_resolver_no_workspace_id_returns_none():
    from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver

    resolver = TurnWorkspaceResolver.__new__(TurnWorkspaceResolver)
    resolver._task = _FakeTaskCrud(_FakeTask(workspace_id=""))
    resolver._workspace = _FakeWorkspaceCrud(None)

    assert resolver.resolve(_FakeTurn(task_id="task-1")) is None


# ----------------------------------------------------------------------
# TurnPrepareService 分支
# ----------------------------------------------------------------------


def _make_service(
    readiness: WorkspaceReadiness,
) -> tuple[TurnPrepareService, _FakeEventService]:
    events = _FakeEventService()
    svc = TurnPrepareService(_FakeLifecycle(readiness), event_service=events)  # type: ignore[arg-type]
    return svc, events


def _readiness(ready: bool, action: str = "init", state: str = "ready") -> WorkspaceReadiness:
    return WorkspaceReadiness(
        ready=ready,
        state=state,
        action_taken=action,
        files_changed=1,
        duration_ms=10,
        degraded_reason=None if ready else "boom",
    )


def test_prepare_ready_emits_preparing_then_ready_and_executes():
    svc, events = _make_service(_readiness(ready=True, action="sync"))
    executed: list[bool] = []

    async def run():
        await svc.prepare_then_execute(
            "task-1", "turn-1", "/ws/root", lambda: _record_executed(executed)
        )

    asyncio.run(run())

    types = [e.event_type for e in events.events]
    assert types == [EventType.WORKSPACE_PREPARING, EventType.WORKSPACE_READY]
    ready_payload = events.events[-1].payload
    assert ready_payload.workspace_path == "/ws/root"
    assert ready_payload.action_taken == "sync"
    assert executed == [True]


def test_prepare_degraded_emits_preparing_then_degraded_and_executes():
    svc, events = _make_service(_readiness(ready=False, state="unavailable"))
    executed: list[bool] = []

    async def run():
        await svc.prepare_then_execute(
            "task-1", "turn-1", "/ws/root", lambda: _record_executed(executed)
        )

    asyncio.run(run())

    types = [e.event_type for e in events.events]
    assert types == [EventType.WORKSPACE_PREPARING, EventType.WORKSPACE_DEGRADED]
    degraded_payload = events.events[-1].payload
    assert degraded_payload.workspace_path == "/ws/root"
    assert degraded_payload.state == "unavailable"
    assert degraded_payload.degraded_reason == "boom"
    # 降级仍放行 execute
    assert executed == [True]


def test_prepare_skipped_when_workspace_path_none():
    svc, events = _make_service(_readiness(ready=True))
    executed: list[bool] = []

    async def run():
        await svc.prepare_then_execute("task-1", "turn-1", None, lambda: _record_executed(executed))

    asyncio.run(run())

    # workspace_path=None 不产生任何准备事件，直接 execute
    assert events.events == []
    assert executed == [True]


# ----------------------------------------------------------------------
# EventType registry 完整性
# ----------------------------------------------------------------------


def test_new_prepare_events_registered():
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_PREPARING] is WorkspacePreparingPayload
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_READY] is WorkspaceReadyPayload
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_DEGRADED] is WorkspaceDegradedPayload


# ----------------------------------------------------------------------
# _drive_turn_with_prepare（API 层接入）与 _emit_run_failed 兜底
# ----------------------------------------------------------------------


class _FakeEventBus:
    """记录 publish / close_turn / release_turn_producer 的假总线。"""

    def __init__(self) -> None:
        self.published: list[Any] = []
        self.closed: list[str] = []
        self.released: list[str] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def close_turn(self, turn_id: str) -> None:
        self.closed.append(turn_id)

    def release_turn_producer(self, turn_id: str) -> None:
        self.released.append(turn_id)


class _FakeRuntime:
    """run_turn 返回空事件流（不真正执行）。"""

    async def _empty_events(self):
        if False:
            yield None

    def run_turn(self, _turn_id: str, turn: Any = None):
        return self._empty_events()


def test_drive_turn_with_prepare_skips_when_prepare_service_none():
    """Kernel 不可用（prepare_service=None）时应跳过准备直接执行，且释放 producer 槽位。"""
    from app.api.turns_api import _drive_turn_with_prepare

    bus = _FakeEventBus()
    runtime = _FakeRuntime()

    async def _run():
        await _drive_turn_with_prepare(
            runtime,  # type: ignore[arg-type]
            "turn-1",
            _FakeTurn(task_id="task-1"),
            bus,  # type: ignore[arg-type]
            prepare_service=None,
        )

    asyncio.run(_run())
    # 跳过准备也完整走完 run_turn 并释放 producer 槽位（幂等，execute 内与最外层均释放）。
    assert "turn-1" in bus.released


def test_emit_run_failed_publishes_terminal_event(monkeypatch):
    """prepare 断开兜底应发布 run_failed 终态事件（§六 验收 5）。"""
    from app.api.turns_api import _emit_run_failed
    from app.models.event.runtime_event import RuntimeEvent

    saved: list[RuntimeEvent] = []

    class _FakeRuntimeEventService:
        def save_and_publish(self, event: RuntimeEvent) -> RuntimeEvent:
            saved.append(event)
            return event

    monkeypatch.setattr("app.api.turns_api.RuntimeEventService", lambda: _FakeRuntimeEventService())
    _emit_run_failed(_FakeTurn(task_id="task-1"), "turn-1")
    assert len(saved) == 1
    assert saved[0].event_type == EventType.RUN_FAILED
    assert saved[0].task_id == "task-1"
    assert saved[0].turn_id == "turn-1"
