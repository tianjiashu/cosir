"""CodeGraph turn 前索引保活迁移后的单元测试。

迁移说明：原 ``TurnPrepareService``（turn 前索引准备）已迁移到内置 Hook
``CodeGraphIndexPrepareHook``（挂载于 ``USER_PROMPT_SUBMIT``，在 run_turn 内部触发）；
API 层 ``_drive_runtime_turn`` 不再做任何前置准备。本文件覆盖：

- ``TurnWorkspaceResolver.resolve``：turn → task → workspace_path 解析（仍被
  workspace 创建即索引链路使用，未删除）。
- ``CodeGraphIndexPrepareHook`` 各分支：无 workspace_id 跳过 / 路径解析失败跳过 /
  空路径跳过 / CodeGraph 不可用（ready=False）放行 / index_sync 成功记日志 /
  异常兜底 ALLOW。
- EventType 新增 3 事件已注册 payload（registry 完整性，workspace lifecycle 事件）。
- ``_drive_runtime_turn``（API 层直接执行）与 ``_emit_run_failed`` 兜底。

不覆盖：真实 Kernel 进程与索引（归端到端冒烟）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.hook.builtins.codegraph_index_prepare_hook import (
    CodeGraphIndexPrepareHook,
)
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.hook.hook_registry import HookRegistry
from app.models.enums.event_type import EventType
from app.models.payload import EVENT_PAYLOAD_MODELS
from app.models.payload.workspace_payload.workspace_degraded_payload import (
    WorkspaceDegradedPayload,
)
from app.models.payload.workspace_payload.workspace_preparing_payload import (
    WorkspacePreparingPayload,
)
from app.models.payload.workspace_payload.workspace_ready_payload import (
    WorkspaceReadyPayload,
)
from app.models.workspace_readiness import WorkspaceReadiness

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


@dataclass
class _FakeWorkspaceRecord:
    workspace_id: str
    root_path: str


class _FakeWorkspaceService:
    def __init__(
        self, record: _FakeWorkspaceRecord | None = None, raise_on_get: bool = False
    ) -> None:
        self._record = record
        self._raise = raise_on_get

    def get_workspace(self, workspace_id: str) -> _FakeWorkspaceRecord:
        if self._raise:
            raise RuntimeError("resolve failed")
        if self._record is None:
            raise KeyError("workspace not found")
        return self._record


class _FakeLifecycle:
    def __init__(self, readiness: WorkspaceReadiness, raise_on_call: bool = False) -> None:
        self._readiness = readiness
        self._raise = raise_on_call
        self.calls: list[str] = []

    def ensure_ready(self, workspace_path: str) -> WorkspaceReadiness:
        self.calls.append(workspace_path)
        if self._raise:
            raise RuntimeError("ensure_ready boom")
        return self._readiness


def _readiness(ready: bool, action: str = "init", state: str = "ready") -> WorkspaceReadiness:
    return WorkspaceReadiness(
        ready=ready,
        state=state,
        action_taken=action,
        files_changed=1,
        duration_ms=10,
        degraded_reason=None if ready else "boom",
    )


def _ctx(workspace_id: str | None = "ws-1", turn_id: str = "turn-1") -> HookContext:
    return HookContext(
        event=HookEvent.USER_PROMPT_SUBMIT,
        workspace_id=workspace_id,
        task_id="task-1",
        turn_id=turn_id,
    )


# ----------------------------------------------------------------------
# TurnWorkspaceResolver（仍被 workspace 创建即索引链路使用）
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
# CodeGraphIndexPrepareHook
# ----------------------------------------------------------------------


def test_hook_skips_when_no_workspace_id():
    hook = CodeGraphIndexPrepareHook(_FakeLifecycle(_readiness(True)), _FakeWorkspaceService())
    decision = hook.execute(_ctx(workspace_id=None))
    assert decision.decision.value == "allow"


def test_hook_skips_when_resolve_fails():
    hook = CodeGraphIndexPrepareHook(
        _FakeLifecycle(_readiness(True)), _FakeWorkspaceService(raise_on_get=True)
    )
    decision = hook.execute(_ctx())
    assert decision.decision.value == "allow"


def test_hook_skips_when_empty_path():
    hook = CodeGraphIndexPrepareHook(
        _FakeLifecycle(_readiness(True)),
        _FakeWorkspaceService(_FakeWorkspaceRecord("ws-1", "")),
    )
    decision = hook.execute(_ctx())
    assert decision.decision.value == "allow"


def test_hook_allows_when_degraded():
    lifecycle = _FakeLifecycle(_readiness(ready=False, state="unavailable"))
    hook = CodeGraphIndexPrepareHook(
        lifecycle, _FakeWorkspaceService(_FakeWorkspaceRecord("ws-1", "/ws/root"))
    )
    decision = hook.execute(_ctx())
    assert decision.decision.value == "allow"
    assert lifecycle.calls == ["/ws/root"]


def test_hook_allows_when_ready():
    lifecycle = _FakeLifecycle(_readiness(ready=True, action="sync"))
    hook = CodeGraphIndexPrepareHook(
        lifecycle, _FakeWorkspaceService(_FakeWorkspaceRecord("ws-1", "/ws/root"))
    )
    decision = hook.execute(_ctx())
    assert decision.decision.value == "allow"
    assert lifecycle.calls == ["/ws/root"]


def test_hook_allows_on_exception():
    lifecycle = _FakeLifecycle(_readiness(True), raise_on_call=True)
    hook = CodeGraphIndexPrepareHook(
        lifecycle, _FakeWorkspaceService(_FakeWorkspaceRecord("ws-1", "/ws/root"))
    )
    decision = hook.execute(_ctx())
    assert decision.decision.value == "allow"


def test_hook_fires_through_registry_matches_and_executes():
    """经 HookInterceptor.fire → matches 链路（非直调 execute），验证 __init__ 正确
    固化基类属性，matches 不抛 AttributeError 且 execute 真正执行。回归：缺
    super().__init__ 时 matches 会抛错，导致 Hook 永不执行。"""
    lifecycle = _FakeLifecycle(_readiness(True, action="sync"))
    hook = CodeGraphIndexPrepareHook(
        lifecycle, _FakeWorkspaceService(_FakeWorkspaceRecord("ws-1", "/ws/root"))
    )
    registry = HookRegistry()
    registry.register(hook)

    # fire 必须成功（不抛），且 ensure_ready 被调用（execute 确实执行）。
    result = HookInterceptor._fire(_ctx(), registry)
    assert result.decision.value == "allow"
    assert lifecycle.calls == ["/ws/root"]


# ----------------------------------------------------------------------
# EventType registry 完整性（workspace lifecycle 事件，仍由 WorkspaceEventService 使用）
# ----------------------------------------------------------------------


def test_workspace_prepare_events_registered():
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_PREPARING] is WorkspacePreparingPayload
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_READY] is WorkspaceReadyPayload
    assert EVENT_PAYLOAD_MODELS[EventType.WORKSPACE_DEGRADED] is WorkspaceDegradedPayload


# ----------------------------------------------------------------------
# _drive_runtime_turn（API 层接入）与 _emit_run_failed 兜底
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


def test_drive_runtime_turn_runs_and_releases():
    """索引准备迁移到 Hook 后，API 层直接执行 run_turn，并释放 producer 槽位。"""
    from app.api.turns_api import _drive_runtime_turn

    bus = _FakeEventBus()
    runtime = _FakeRuntime()

    async def _run():
        await _drive_runtime_turn(
            runtime,  # type: ignore[arg-type]
            "turn-1",
            _FakeTurn(task_id="task-1"),
            bus,  # type: ignore[arg-type]
        )

    asyncio.run(_run())
    # 直接走完 run_turn 并释放 producer 槽位（execute 内与最外层均释放）。
    assert "turn-1" in bus.released


def test_emit_run_failed_publishes_terminal_event(monkeypatch):
    """run 未启动即断开兜底应发布 run_failed 终态事件。"""
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
