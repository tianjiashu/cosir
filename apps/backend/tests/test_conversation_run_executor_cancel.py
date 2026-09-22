"""ConversationRunExecutor 的进程内取消信号与启动闸门测试。"""

import asyncio
import threading
from collections import deque
from types import SimpleNamespace

import pytest

import app.assistant_transport.service.conversation_run_executor as executor_module
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry

_RUN_ID = 1


class _RunService:
    """只实现执行器读取 run 所需的最小语义。"""

    def __init__(self, status: str = "running") -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=7, status=status)

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return self.run


def _delegation(child_run_id: int | None) -> SimpleNamespace:
    """构造只带 child run 绑定的委派记录替身。"""

    return SimpleNamespace(child_run_id=child_run_id)


class _FakeDelegationService:
    """用静态委派表复刻 ``list_active_by_parent_turn`` 的语义（只返回仍活跃的委派）。"""

    def __init__(
        self,
        records_by_parent: dict[int, list[object]] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        """记录委派表、查询轨迹与是否模拟查询失败。"""

        self._records_by_parent = records_by_parent or {}
        self._raises = raises
        self.queried_parent_run_ids: list[int] = []

    def list_active_by_parent_turn(self, parent_run_id: int) -> list[object]:
        """返回该父 run 下的活跃委派记录；``raises`` 时抛异常以验证兜底。"""

        self.queried_parent_run_ids.append(parent_run_id)
        if self._raises:
            raise RuntimeError("delegation query failed")
        return self._records_by_parent.get(parent_run_id, [])


class _FakeChildSessions:
    """Explicit child-session contract used by executor cancellation tests."""

    def __init__(self, delegation_service: _FakeDelegationService) -> None:
        self._delegation_service = delegation_service
        self.cancel_descendant_thread_ids: list[int] = []

    def cancel_descendants(self, run_id: int) -> None:
        self.cancel_descendant_thread_ids.append(threading.get_ident())
        visited: set[int] = {run_id}
        queue: deque[int] = deque([run_id])
        while queue:
            current_run_id = queue.popleft()
            for record in self._delegation_service.list_active_by_parent_turn(current_run_id):
                child_run_id = record.child_run_id
                if not child_run_id or child_run_id in visited:
                    continue
                visited.add(child_run_id)
                queue.append(child_run_id)
                cancellation_registry.mark_cancelled(child_run_id)

    def close_children(self, _run_id: int) -> None:
        return None

    def sweep_pending_follow_ups(self) -> int:
        return 0

    def shutdown(self) -> None:
        return None


def _build_executor(
    service: object, delegation_service: object | None = None
) -> ConversationRunExecutor:
    """绕过依赖装配构造只注入 run service、进程内信号源与委派 service 替身的执行器实例。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._signal = cancellation_registry
    executor._event_projector = None
    executor._delegation_service = (
        delegation_service if delegation_service is not None else _FakeDelegationService()
    )
    executor._child_sessions = _FakeChildSessions(executor._delegation_service)
    executor._executions = {}
    return executor


@pytest.fixture(autouse=True)
def _isolated_child_run_signals():
    """每个用例前后清空本文件用到的子 run 信号，避免用例间串扰。"""

    for run_id in (11, 12, 13):
        cancellation_registry.clear(run_id)
    yield
    for run_id in (11, 12, 13):
        cancellation_registry.clear(run_id)


@pytest.fixture(autouse=True)
def _isolated_signal_registry():
    """每个用例前后清空进程内取消信号，避免用例间串扰。"""

    cancellation_registry.clear(_RUN_ID)
    yield
    cancellation_registry.clear(_RUN_ID)


@pytest.mark.asyncio
async def test_cancel_marks_signal_and_returns_without_waiting_for_runner() -> None:
    """runner 永不返回时取消依旧立即返回 True，且只标记进程内信号。"""

    service = _RunService(status="running")
    executor = _build_executor(service)

    async def runner(_run: object) -> None:
        await asyncio.Event().wait()

    execution = await executor.start(_RUN_ID, runner)
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
    executor = _build_executor(service)
    entered = threading.Event()
    release = threading.Event()
    close_thread_id: list[int] = []

    class _BlockingTerminalService:
        def close_run_terminals(self, run_id: int, *, reason: str) -> None:
            close_thread_id.append(threading.get_ident())
            entered.set()
            release.wait()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        executor_module,
        "get_terminal_session_service",
        lambda: _BlockingTerminalService(),
    )
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
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_cancel_runs_descendant_crud_off_event_loop() -> None:
    delegation_service = _FakeDelegationService({_RUN_ID: [_delegation(11)]})
    child_sessions = _FakeChildSessions(delegation_service)
    executor = _build_executor(_RunService(status="running"), delegation_service)
    executor._child_sessions = child_sessions
    event_loop_thread_id = threading.get_ident()

    assert await executor.cancel(_RUN_ID) is True

    assert child_sessions.cancel_descendant_thread_ids
    assert all(
        thread_id != event_loop_thread_id
        for thread_id in child_sessions.cancel_descendant_thread_ids
    )


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
async def test_cancel_cascades_to_descendant_runs_breadth_first() -> None:
    """父 run 取消后沿委派关系级联：广度优先，且只标记活跃且已绑定 child run 的委派。"""

    delegation_service = _FakeDelegationService(
        {
            _RUN_ID: [_delegation(11), _delegation(None), _delegation(12)],
            11: [_delegation(13)],
        }
    )
    executor = _build_executor(_RunService(status="running"), delegation_service)

    assert await executor.cancel(_RUN_ID) is True
    assert cancellation_registry.is_cancelled(11) is True
    assert cancellation_registry.is_cancelled(12) is True
    # 更深一层同样被覆盖，说明遍历会下探后代。
    assert cancellation_registry.is_cancelled(13) is True
    assert delegation_service.queried_parent_run_ids == [_RUN_ID, 11, 12, 13]


@pytest.mark.asyncio
async def test_duplicate_cancel_does_not_recascade() -> None:
    """重复取消在第一步短路：既不重复标记父 run，也不重复查询委派关系。"""

    delegation_service = _FakeDelegationService({_RUN_ID: [_delegation(11)]})
    executor = _build_executor(_RunService(status="running"), delegation_service)

    assert await executor.cancel(_RUN_ID) is True
    assert await executor.cancel(_RUN_ID) is False
    # 首次取消：查父 run 与已标记的子 run（下探其后代）；第二次在第一步短路，不再查询。
    assert delegation_service.queried_parent_run_ids == [_RUN_ID, 11]


@pytest.mark.asyncio
async def test_cancel_cascade_terminates_on_cycle() -> None:
    """数据异常成环（子 run 又指向父 run）时只查询一次、不重复标记、不死循环。"""

    delegation_service = _FakeDelegationService(
        {_RUN_ID: [_delegation(11)], 11: [_delegation(_RUN_ID)]}
    )
    executor = _build_executor(_RunService(status="running"), delegation_service)

    assert await executor.cancel(_RUN_ID) is True
    assert cancellation_registry.is_cancelled(11) is True
    assert delegation_service.queried_parent_run_ids == [_RUN_ID, 11]


@pytest.mark.asyncio
async def test_cancel_survives_cascade_query_failure() -> None:
    """级联查询失败不得让父 run 的取消请求失败：信号已标记，返回值仍为 True。"""

    executor = _build_executor(_RunService(status="running"), _FakeDelegationService(raises=True))

    assert await executor.cancel(_RUN_ID) is True
    assert cancellation_registry.is_cancelled(_RUN_ID) is True


@pytest.mark.asyncio
async def test_cancel_cascade_base_exception_escapes_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """级联边界只兜 ``Exception``：``BaseException``（如 KeyboardInterrupt）仍向外传播。

    需要让进程中断的异常不允许被静默吞掉；父信号此时已标记，故调用方仍可据信号继续收口。
    """

    real_mark = cancellation_registry.mark_cancelled

    def _mark(run_id: int) -> None:
        if run_id == 11:
            raise KeyboardInterrupt("registry backend interrupted")
        real_mark(run_id)

    monkeypatch.setattr(cancellation_registry, "mark_cancelled", _mark)
    delegation_service = _FakeDelegationService({_RUN_ID: [_delegation(11)]})
    executor = _build_executor(_RunService(status="running"), delegation_service)

    with pytest.raises(KeyboardInterrupt):
        await executor.cancel(_RUN_ID)

    assert cancellation_registry.is_cancelled(_RUN_ID) is True


@pytest.mark.asyncio
async def test_cancel_does_not_cascade_when_run_missing() -> None:
    """404 路径（run 不存在）不得级联：信号回滚，且不白查一次委派关系。"""

    class _MissingRunService(_RunService):
        def get_run(self, _run_id: int) -> SimpleNamespace:
            raise KeyError(_run_id)

    delegation_service = _FakeDelegationService()
    executor = _build_executor(_MissingRunService(), delegation_service)

    with pytest.raises(KeyError):
        await executor.cancel(_RUN_ID)
    assert delegation_service.queried_parent_run_ids == []


@pytest.mark.asyncio
async def test_start_rejects_run_that_is_not_running() -> None:
    """run 状态不是 running 时拒绝启动执行器，且不登记执行注册。"""

    executor = _build_executor(_RunService(status="cancelled"))

    async def runner(_run: object) -> None:
        return None

    with pytest.raises(ValueError):
        await executor.start(_RUN_ID, runner)
    assert executor._executions == {}
