"""ConversationRunExecutor 的进程内取消信号与启动闸门测试。"""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.runtime.conversation_run_executor as executor_module
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.conversation_run_executor import ConversationRunExecutor, _Execution

_RUN_ID = 1
_TASK_ID = 7


class _RunService:
    """只实现执行器读取 run 所需的最小语义。"""

    def __init__(self, status: str = "running") -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID, status=status)

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return self.run


def _build_executor(service: object) -> ConversationRunExecutor:
    """绕过依赖装配构造执行器实例，显式注入它在 ``__init__`` 中捕获的协作者。

    ``get_runtime`` 是调用期解析（见 ``_patch_drivers``），不需要注入。
    """

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._task_service = service
    executor._signal = cancellation_registry
    executor._event_projector = None
    executor._executions = {}
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
) -> None:
    """把 ``get_runtime`` 替换为可控替身。"""

    monkeypatch.setattr(executor_module, "get_runtime", lambda: _StubRuntime(behavior))


@pytest.fixture(autouse=True)
def _isolated_signal_registry():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.consume(_TASK_ID)
    yield
    cancellation_registry.consume(_TASK_ID)


@pytest.mark.asyncio
async def test_cancel_marks_signal_and_returns_without_waiting_for_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 永不返回时取消依旧立即返回 True，且只标记进程内信号。"""

    service = _RunService(status="running")
    executor = _build_executor(service)

    async def runner() -> None:
        await asyncio.Event().wait()

    _patch_drivers(monkeypatch, runner)

    execution = await executor.start(_RUN_ID, "fresh")
    try:
        assert await asyncio.wait_for(executor.cancel(_TASK_ID), timeout=1.0) is True
        assert cancellation_registry.is_cancelled(_TASK_ID) is True
        # 取消只发信号：不落库 run 终态，也不取消后台执行 task。
        assert service.run.status == "running"
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_preserves_task_terminal(monkeypatch, tmp_path) -> None:
    from test_terminal_session_service import make_service

    terminal, workers = make_service(monkeypatch)
    session = terminal.start(task_id=7, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    executor = _build_executor(_RunService(status="running"))
    executor._executions[_RUN_ID] = _Execution(
        task_id=_TASK_ID, thread_task=asyncio.current_task()
    )
    try:
        assert await executor.cancel(_TASK_ID) is True
        assert not workers[0].closed
        terminal.write(
            str(session["session_id"]),
            task_id=7,
            input={"type": "line", "text": "next run"},
            operation_id="after-run-cancel",
            wait_ms=0,
        )
        assert workers[0].writes == [b"next run\r"]
    finally:
        terminal.shutdown()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_while_execution_is_active() -> None:
    """同一 Task 重复停止都被接受，信号只有一份。"""

    executor = _build_executor(_RunService(status="running"))
    executor._executions[_RUN_ID] = _Execution(
        task_id=_TASK_ID, thread_task=asyncio.current_task()
    )

    assert await executor.cancel(_TASK_ID) is True
    assert await executor.cancel(_TASK_ID) is True
    assert executor._release(_RUN_ID) is True
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


@pytest.mark.asyncio
async def test_cancel_missing_task_raises_without_signal() -> None:
    """task 不存在时抛 KeyError（API 映射 404），且不登记信号。"""

    class _MissingTaskService(_RunService):
        def get_task(self, task_id: int) -> SimpleNamespace:
            raise KeyError(task_id)

    executor = _build_executor(_MissingTaskService())

    with pytest.raises(KeyError):
        await executor.cancel(_TASK_ID)
    assert cancellation_registry.is_cancelled(_TASK_ID) is False


@pytest.mark.asyncio
async def test_start_rejects_run_that_is_not_running() -> None:
    """run 状态不是 running 时拒绝启动执行器，且不登记执行注册。"""

    executor = _build_executor(_RunService(status="cancelled"))

    with pytest.raises(ValueError):
        await executor.start(_RUN_ID, "fresh")
    assert executor._executions == {}
