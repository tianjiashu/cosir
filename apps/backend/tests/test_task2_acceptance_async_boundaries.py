"""Task 2 final-acceptance regressions for async boundaries and finalization failures."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

import app.lifespan as lifespan_module
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.enums.conversation_run_status import ConversationRunStatus

_RUN_ID = 7001


class _ThreadCheckedRunService:
    def __init__(self, *, status: str = ConversationRunStatus.RUNNING.value) -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=7, status=status)
        self.owner_thread = threading.get_ident()
        self.get_calls = 0
        # actor 把信号处理异常记录后吞掉：违例另行记下，用例据此断言。
        self.violations: list[str] = []
        self.reads: list[str] = []

    def get_run(self, _run_id: int) -> SimpleNamespace:
        self.get_calls += 1
        if threading.get_ident() == self.owner_thread:
            raise AssertionError("canonical get_run ran on the event-loop thread")
        return self.run

    def get_task(self, task_id: int) -> SimpleNamespace:
        self._assert_off_loop("get_task")
        return SimpleNamespace(id=task_id)

    def get_latest_run(self, _task_id: int) -> SimpleNamespace:
        self._assert_off_loop("get_latest_run")
        return self.run

    def require_latest_run(self, _task_id: int) -> SimpleNamespace:
        self._assert_off_loop("require_latest_run")
        return self.run

    def _assert_off_loop(self, name: str) -> None:
        self.reads.append(name)
        if threading.get_ident() == self.owner_thread:
            self.violations.append(name)
            raise AssertionError(f"canonical {name} ran on the event-loop thread")

    def transition(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_run_signal() -> None:
    cancellation_registry.consume(7)
    yield
    cancellation_registry.consume(7)


@pytest.mark.asyncio
async def test_session_cancel_reads_canonical_run_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from app.service import depends as service_depends
    from app.service.conversation_run import task_actor as actor_module
    from app.service.conversation_run.task_actors import TaskActors

    monkeypatch.setattr(
        actor_module.task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(async_operation=lambda **kw: nullcontext()),
    )
    service = _ThreadCheckedRunService(status=ConversationRunStatus.SUSPENDED.value)
    for getter in (
        "get_conversation_run_service",
        "get_conversation_run_state_service",
        "get_task_service",
        "get_task_message_queue",
        "get_user_input_service",
    ):
        monkeypatch.setattr(service_depends, getter, lambda: service)
    session = TaskActors()

    # 无执行者时由 actor 读 canonical Run 判定是否挂起；所有读取都必须离开事件循环线程。
    try:
        actor = session._actor(7)
        changed = actor.state_changed
        session.cancel(7)
        await asyncio.wait_for(changed.wait(), 1)
        assert "require_latest_run" in service.reads
        assert service.violations == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_startup_failure_dependency_cleanup_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_thread = threading.get_ident()
    calls: list[str] = []

    class _Executor:
        async def close(self) -> None:
            calls.append("executor_close")

    class _Terminal:
        def shutdown(self) -> None:
            calls.append("terminal_shutdown")

    class _Getter:
        def __init__(self, value: object) -> None:
            self.value = value

        def cache_info(self) -> SimpleNamespace:
            return SimpleNamespace(currsize=1)

        def __call__(self) -> object:
            return self.value

    def close_dependencies() -> None:
        assert threading.get_ident() != owner_thread
        calls.append("close_dependencies")

    monkeypatch.setattr(lifespan_module, "get_task_actors", _Getter(_Executor()))
    monkeypatch.setattr(lifespan_module, "get_terminal_session_service", _Getter(_Terminal()))
    monkeypatch.setattr(lifespan_module, "close_service_dependencies", close_dependencies)

    await lifespan_module._cleanup_startup_failure()

    assert calls == ["executor_close", "terminal_shutdown", "close_dependencies"]


@pytest.mark.asyncio
async def test_normal_shutdown_dependency_cleanup_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_thread = threading.get_ident()
    calls: list[str] = []

    class _RunService:
        def recover_orphaned_runs(self) -> list[object]:
            return []

        def list_latest_runs(self) -> list[object]:
            return []

    class _Terminal:
        def initialize(self) -> None:
            return None

        def shutdown(self) -> None:
            calls.append("terminal_shutdown")

    class _Executor:
        async def close(self) -> None:
            calls.append("executor_close")

    class _ToolSystem:
        @classmethod
        def build_tool_system(cls) -> object:
            return object()

    monkeypatch.setattr(
        lifespan_module, "install_logging_for_current_process", lambda **_kwargs: None
    )
    monkeypatch.setattr(lifespan_module, "shutdown_logging", lambda: None)
    monkeypatch.setattr(lifespan_module.Settings, "load", staticmethod(lambda: None))
    monkeypatch.setattr(lifespan_module, "initialize_service_dependencies", lambda: None)
    monkeypatch.setattr(lifespan_module, "_ensure_system_cosir_dir", lambda: None)
    monkeypatch.setattr(lifespan_module, "ensure_system_agent_config_dir", lambda: None)
    monkeypatch.setattr(lifespan_module, "ensure_system_agent_team_config_dir", lambda: None)
    monkeypatch.setattr(lifespan_module, "get_conversation_run_service", lambda: _RunService())
    monkeypatch.setattr(lifespan_module, "get_terminal_session_service", lambda: _Terminal())
    monkeypatch.setattr(lifespan_module, "get_task_actors", lambda: _Executor())
    monkeypatch.setattr(lifespan_module, "ToolSystem", _ToolSystem)
    # Registry 只保存内置 profile，用户 JSON 由注册表在作用域首次被读取时按需装载，
    # 因此启动编排不再遍历 workspace 调用装载方法。
    monkeypatch.setattr(lifespan_module, "build_agent_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifespan_module, "set_tool_system", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "set_agent_registry", lambda _value: None)
    monkeypatch.setattr(lifespan_module.HookInterceptor, "safe_fire", lambda *_args: None)
    monkeypatch.setattr("app.core.hook.initialize_hook_registry", lambda: None)

    def close_dependencies() -> None:
        assert threading.get_ident() != owner_thread
        calls.append("close_dependencies")

    monkeypatch.setattr(lifespan_module, "close_service_dependencies", close_dependencies)
    # 启动流程里的 Agent Team 收敛在函数内惰性 import，因此按模块路径打桩（替身不触达存储）。
    monkeypatch.setattr(
        "app.service.agent_team.coordinator.get_agent_team_coordinator",
        lambda: SimpleNamespace(recover_after_restart=lambda: []),
    )

    async with lifespan_module._lifespan_impl(SimpleNamespace()):
        pass

    assert calls[-2:] == ["terminal_shutdown", "close_dependencies"]
