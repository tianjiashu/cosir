"""ConversationRunExecutor 的进程内取消信号与启动闸门测试。"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.runtime.conversation_run_executor as executor_module
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import ConversationRunExecutor

_RUN_ID = 1


class _RunService:
    """只实现执行器读取 run 所需的最小语义。"""

    def __init__(self, status: str = "running") -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=7, status=status)

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return self.run


class _TerminalStub:
    """terminal session service 替身：签名与真实服务一致（``reason`` 为 keyword-only）。"""

    def begin_run(self, _run_id: int) -> None:
        return None

    def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
        return None


def _build_executor(service: object, terminal: object | None = None) -> ConversationRunExecutor:
    """绕过依赖装配构造执行器实例，显式注入它在 ``__init__`` 中捕获的协作者。

    ``get_runtime`` 是调用期解析（见 ``_patch_drivers``），不需要注入。
    """

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._signal = cancellation_registry
    executor._event_projector = None
    executor._executions = {}
    executor._terminal_session_service = terminal or _TerminalStub()
    return executor


class _StubRuntime:
    """替代真实 AgentRuntime：``run_agent`` 的驱动行为由用例注入。"""

    def __init__(self, behavior: Callable[[], Awaitable[None]]) -> None:
        self._behavior = behavior

    def resolve_agent_profile_for_run(self, _run: Any) -> Any:
        return object()

    async def run_agent(self, _agent: Any, **_kwargs: Any) -> None:
        await self._behavior()


def _patch_drivers(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[], Awaitable[None]],
    *,
    executor: ConversationRunExecutor,
    terminal: object | None = None,
) -> None:
    """把 ``get_runtime`` 替换为可控替身，并把 terminal service 替身注入执行器。"""

    monkeypatch.setattr(executor_module, "get_runtime", lambda: _StubRuntime(behavior))
    executor._terminal_session_service = terminal or _TerminalStub()


@pytest.fixture(autouse=True)
def _isolated_signal_registry():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.clear(_RUN_ID)
    yield
    cancellation_registry.clear(_RUN_ID)


@pytest.mark.asyncio
async def test_cancel_marks_signal_and_returns_without_waiting_for_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 永不返回时取消依旧立即返回 True，且只标记进程内信号。"""

    service = _RunService(status="running")
    executor = _build_executor(service)

    async def runner() -> None:
        await asyncio.Event().wait()

    _patch_drivers(monkeypatch, runner, executor=executor)

    execution = await executor.start(_RUN_ID, "fresh")
    try:
        assert await asyncio.wait_for(executor.cancel(_RUN_ID), timeout=1.0) is True
        assert cancellation_registry.is_cancelled(_RUN_ID) is True
        # 取消只发信号：不落库 run 终态，也不取消后台执行 task。
        assert service.run.status == "running"
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_closes_terminals_off_event_loop() -> None:
    service = _RunService(status="running")
    entered = threading.Event()
    release = threading.Event()
    close_thread_id: list[int] = []

    class _BlockingTerminalService(_TerminalStub):
        def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
            close_thread_id.append(threading.get_ident())
            entered.set()
            release.wait()

    executor = _build_executor(service, _BlockingTerminalService())
    try:
        cancel_task = asyncio.create_task(executor.cancel(_RUN_ID))
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
        heartbeat = asyncio.Event()

        async def tick() -> None:
            await asyncio.sleep(0)
            heartbeat.set()

        tick_task = asyncio.create_task(tick())
        await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        release.set()
        assert await cancel_task is True
        await tick_task
        assert close_thread_id
        assert close_thread_id[0] != threading.get_ident()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_for_already_marked_run() -> None:
    """同一 run 重复取消返回 False，不重复标记。"""

    executor = _build_executor(_RunService(status="running"))

    assert await executor.cancel(_RUN_ID) is True
    assert await executor.cancel(_RUN_ID) is False


@pytest.mark.asyncio
async def test_cancel_rolls_back_signal_when_run_missing() -> None:
    """run 不存在时撤销已标记的信号并抛 KeyError，API 据此映射 404。"""

    class _MissingRunService(_RunService):
        def get_run(self, _run_id: int) -> SimpleNamespace:
            raise KeyError(_run_id)

    executor = _build_executor(_MissingRunService())

    with pytest.raises(KeyError):
        await executor.cancel(_RUN_ID)
    assert cancellation_registry.is_cancelled(_RUN_ID) is False


@pytest.mark.asyncio
async def test_start_rejects_run_that_is_not_running() -> None:
    """run 状态不是 running 时拒绝启动执行器，且不登记执行注册。"""

    executor = _build_executor(_RunService(status="cancelled"))

    with pytest.raises(ValueError):
        await executor.start(_RUN_ID, "fresh")
    assert executor._executions == {}
