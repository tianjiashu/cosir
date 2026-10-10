"""Task 2 final-acceptance regressions for async boundaries and finalization failures."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.runtime.conversation_run_executor as executor_module
import app.lifespan as lifespan_module
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import ConversationRunExecutor
from app.models.enums.conversation_run_status import ConversationRunStatus

_RUN_ID = 7001


class _ThreadCheckedRunService:
    def __init__(self, *, status: str = ConversationRunStatus.RUNNING.value) -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=7, status=status)
        self.owner_thread = threading.get_ident()
        self.get_calls = 0

    def get_run(self, _run_id: int) -> SimpleNamespace:
        self.get_calls += 1
        if threading.get_ident() == self.owner_thread:
            raise AssertionError("canonical get_run ran on the event-loop thread")
        return self.run

    def cancel_run_if_running(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def fail_run_if_running(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ThreadRecordingRunService(_ThreadCheckedRunService):
    """记录每次 canonical 读取所在线程，用于断言收束路径不在事件循环线程读取。"""

    def __init__(self) -> None:
        super().__init__()
        self.read_threads: list[int] = []

    def get_run(self, _run_id: int) -> SimpleNamespace:
        self.get_calls += 1
        self.read_threads.append(threading.get_ident())
        return self.run


class _StubRuntime:
    """替代真实 AgentRuntime：``run_agent`` 的驱动行为由用例注入。"""

    def __init__(self, behavior: Callable[[], Awaitable[None]]) -> None:
        self._behavior = behavior

    def resolve_agent_profile_for_run(self, _run: Any) -> Any:
        return object()

    async def run_agent(self, _agent: Any, **_kwargs: Any) -> None:
        await self._behavior()


class _Terminal:
    """terminal session service 替身：签名与真实服务一致（``reason`` 为 keyword-only）。"""

    def begin_run(self, _run_id: int) -> None:
        return None

    def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
        return None


def _executor(run_service: object) -> ConversationRunExecutor:
    """构造执行器替身：显式注入它在 ``__init__`` 中捕获的 run service 与 terminal service。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = run_service
    executor._run_state_service = run_service
    executor._event_projector = None
    executor._signal = cancellation_registry
    executor._executions = {}
    executor._terminal_session_service = _Terminal()
    return executor


def _patch_execute_drivers(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[], Awaitable[None]],
    *,
    executor: ConversationRunExecutor,
) -> None:
    """把执行器依赖的 runtime 替换为可控替身（terminal service 由替身实例注入）。"""

    monkeypatch.setattr(executor_module, "get_runtime", lambda: _StubRuntime(behavior))
    executor._terminal_session_service = _Terminal()


@pytest.fixture(autouse=True)
def _clear_run_signal() -> None:
    cancellation_registry.clear(_RUN_ID)
    yield
    cancellation_registry.clear(_RUN_ID)


@pytest.mark.asyncio
async def test_executor_start_reads_canonical_run_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    async def runner() -> None:
        return None

    _patch_execute_drivers(monkeypatch, runner, executor=executor)

    task = await executor.start(_RUN_ID, "fresh")
    await task


@pytest.mark.asyncio
async def test_executor_tool_settlement_reads_canonical_run_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具收束路径读取 canonical run 时不得占用事件循环线程。"""

    service = _ThreadRecordingRunService()
    executor = _executor(service)
    executor._event_projector = SimpleNamespace(process=lambda _event: None)
    loop_thread = threading.get_ident()

    async def failing_runner() -> None:
        raise RuntimeError("runner failed")

    _patch_execute_drivers(monkeypatch, failing_runner, executor=executor)

    # ``_execute`` 内部收口驱动期异常，工具收束投影仍在同一路径上执行。
    await executor._execute(_RUN_ID, "fresh")

    # 首次读取发生在 ``_execute`` 的同步上下文；工具收束经 to_thread 读取，必须落在其它线程。
    assert any(thread_id != loop_thread for thread_id in service.read_threads)


@pytest.mark.asyncio
async def test_executor_cancel_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    assert await executor.cancel(_RUN_ID) is True


@pytest.mark.asyncio
async def test_executor_cancel_tool_call_reads_canonical_run_off_event_loop() -> None:
    service = _ThreadCheckedRunService()
    executor = _executor(service)

    assert await executor.cancel_tool_call(_RUN_ID, "tool-1") is True


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

    monkeypatch.setattr(lifespan_module, "get_conversation_run_executor", _Getter(_Executor()))
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

    class _RuntimeFactory:
        def __init__(self) -> None:
            return None

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
    monkeypatch.setattr(lifespan_module, "get_conversation_run_executor", lambda: _Executor())
    monkeypatch.setattr(lifespan_module, "ToolSystem", _ToolSystem)
    monkeypatch.setattr(lifespan_module, "ToolRuntimeOutputChannelFactory", _RuntimeFactory)
    # Registry 只保存内置 profile，用户 JSON 由注册表在作用域首次被读取时按需装载，
    # 因此启动编排不再遍历 workspace 调用装载方法。
    monkeypatch.setattr(lifespan_module, "build_agent_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(lifespan_module, "set_tool_system", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "set_agent_registry", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "set_runtime", lambda _value: None)
    monkeypatch.setattr(lifespan_module, "AgentRuntime", lambda **_kwargs: object())
    monkeypatch.setattr(lifespan_module, "flush_langfuse", lambda: None)
    monkeypatch.setattr(lifespan_module.HookInterceptor, "safe_fire", lambda *_args: None)
    monkeypatch.setattr("app.core.hook.initialize_hook_registry", lambda: None)

    def close_dependencies() -> None:
        assert threading.get_ident() != owner_thread
        calls.append("close_dependencies")

    monkeypatch.setattr(lifespan_module, "close_service_dependencies", close_dependencies)
    # 启动流程里的 Agent Team 收敛在函数内惰性 import，因此按模块路径打桩（替身不触达存储）。
    monkeypatch.setattr(
        "app.agent_team.coordinator.get_agent_team_coordinator",
        lambda: SimpleNamespace(recover_after_restart=lambda: []),
    )

    async with lifespan_module._lifespan_impl(SimpleNamespace()):
        pass

    assert calls[-2:] == ["terminal_shutdown", "close_dependencies"]


def test_executor_does_not_own_child_sessions() -> None:
    """[契约] 执行器不再持有子会话服务：子 Agent 收敛已整体移除。"""

    executor = _executor(_ThreadCheckedRunService())

    assert not hasattr(executor, "_child_sessions")
