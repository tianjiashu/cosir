"""僵尸 running 防治测试：executor 兜底收敛 + 命令服务 claim 后失败当场收敛。

对应修复的不变量：**谁把 run 翻成 active，谁就负责保证它最终落终态**——要么成功
移交能保证落终态的执行体（workflow / executor），要么当场收敛。覆盖两条链路：

- ``ConversationRunExecutor._execute``：驱动结束（runner 抛错 / 被取消）但 run 仍
  pending/running 时，经 ``_converge_unfinished_run`` 条件收敛为 failed/cancelled；
- ``ConversationRunCommandService``：``start_or_attach`` / ``edit_or_restart`` 事务
  提交后的投影、认领、快照重读任一步失败时，以 ``run_setup_failed`` 当场收敛再抛出。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import app.assistant_transport.service.conversation_run_executor as executor_module
from app.assistant_transport.event import RunInitializedEvent
from app.assistant_transport.service import conversation_run_command_service as command_module
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandService,
)
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.config.logging.logger import log

_RUN_ID = 1
_TASK_ID = 7


# --------------------------------------------------------------------------- executor


class _ExecutorRunService:
    """复刻执行器所需的最小 run service 语义，记录两条条件收敛入口的调用。"""

    def __init__(self, status: str = "running") -> None:
        self.run = SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID, status=status)
        self.failed: list[tuple[int, str | None]] = []
        self.cancelled: list[tuple[int, str]] = []

    def get_run(self, _run_id: int) -> SimpleNamespace:
        return self.run

    def fail_run_if_running(
        self, run_id: int, end_reason: str | None = None, **_kwargs: Any
    ) -> SimpleNamespace:
        self.failed.append((run_id, end_reason))
        return self.run

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "user_cancelled", **_kwargs: Any
    ) -> SimpleNamespace:
        self.cancelled.append((run_id, end_reason))
        return self.run


def _build_executor(service: _ExecutorRunService) -> ConversationRunExecutor:
    """绕过依赖装配构造只注入 run service 的执行器实例（projector 置空跳过工具投影）。"""

    executor = ConversationRunExecutor.__new__(ConversationRunExecutor)
    executor._run_service = service
    executor._event_projector = None
    executor._child_sessions = SimpleNamespace(
        close_children=lambda _run_id: None,
        sweep_pending_follow_ups=lambda: 0,
        cancel_descendants=lambda _run_id: None,
        shutdown=lambda: None,
    )
    executor._executions = {}
    return executor


@pytest.mark.asyncio
async def test_executor_converges_run_when_runner_raises_before_terminal() -> None:
    """runner（含 workflow 进入前的 setup 步骤）抛错且 run 未落终态时，兜底收敛为 failed。"""

    service = _ExecutorRunService()
    executor = _build_executor(service)

    async def runner(_run: object) -> None:
        raise RuntimeError("setup died before workflow")

    with pytest.raises(RuntimeError, match="setup died before workflow"):
        await executor._execute(_RUN_ID, runner)

    assert service.failed == [(_RUN_ID, "run_execution_ended_without_terminal")]
    assert service.cancelled == []


@pytest.mark.asyncio
async def test_executor_converges_run_when_task_cancelled() -> None:
    """后台 task 被取消（优雅关闭路径）时，兜底收敛为 cancelled 而不是遗留 running。"""

    service = _ExecutorRunService()
    executor = _build_executor(service)

    async def runner(_run: object) -> None:
        await asyncio.Event().wait()

    execution = await executor.start(_RUN_ID, runner)
    # 先让后台 task 真正开始驱动，再取消：未调度就取消会让 _execute 整体不执行。
    await asyncio.sleep(0.05)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert service.cancelled == [(_RUN_ID, "run_execution_cancelled")]
    assert service.failed == []


@pytest.mark.asyncio
async def test_executor_converge_failure_does_not_mask_runner_error() -> None:
    """兜底收敛自身失败只记日志：不得覆盖 runner 的原始异常，也不阻断收尾。"""

    class _BrokenRunService(_ExecutorRunService):
        def fail_run_if_running(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("database unavailable")

    executor = _build_executor(_BrokenRunService())

    async def runner(_run: object) -> None:
        raise RuntimeError("runner failed")

    with pytest.raises(RuntimeError, match="runner failed"):
        await executor._execute(_RUN_ID, runner)


@pytest.mark.asyncio
async def test_executor_cleanup_failures_do_not_mask_runner_or_skip_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 原始异常优先，child cleanup 失败也不能跳过 terminal、registry、convergence。"""

    service = _ExecutorRunService()
    calls: list[str] = []

    class _ChildSessions:
        def close_children(self, _run_id: int) -> None:
            calls.append("child_close")
            raise RuntimeError("child cleanup failed")

        def sweep_pending_follow_ups(self) -> int:
            calls.append("child_sweep")
            return 0

    class _Terminal:
        def begin_run(self, _run_id: int) -> None:
            calls.append("terminal_begin")

        def close_run_terminals(self, _run_id: int, *, reason: str) -> None:
            calls.append(f"terminal_close:{reason}")

    executor = _build_executor(service)
    executor._child_sessions = _ChildSessions()
    monkeypatch.setattr(executor_module, "get_terminal_session_service", lambda: _Terminal())

    async def runner(_run: object) -> None:
        executor._executions[_RUN_ID] = SimpleNamespace(thread_task=asyncio.current_task())
        calls.append("runner")
        raise RuntimeError("runner failed")

    with pytest.raises(RuntimeError, match="runner failed"):
        await executor._execute(_RUN_ID, runner)

    assert calls == [
        "terminal_begin",
        "runner",
        "child_close",
        "child_sweep",
        "terminal_close:run_execution_finished",
    ]
    assert executor._executions == {}
    assert service.failed == [(_RUN_ID, "run_execution_ended_without_terminal")]


# --------------------------------------------------------------------- command service


class _FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeSessionFactory:
    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()


class _CommandServiceRunState:
    """记录命令服务侧用到的状态迁移语义。"""

    def __init__(self, *, claim_raises: bool = False) -> None:
        self.claim_raises = claim_raises
        self.settled: list[tuple[int, str]] = []
        self.claimed: list[int] = []

    def has_active_run(self, _task_id: int, session: object = None) -> bool:
        return False

    def claim_pending_run(self, run_id: int) -> SimpleNamespace:
        if self.claim_raises:
            raise RuntimeError("claim dispatch failed")
        self.claimed.append(run_id)
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status="running")

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "user_cancelled", **_kwargs: Any
    ) -> SimpleNamespace:
        self.settled.append((run_id, end_reason))
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status="cancelled")


class _Projector:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    def process(self, _event: object, **_kwargs: Any) -> None:
        if self._raises:
            raise RuntimeError("projection failed")


def _build_command_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_state: _CommandServiceRunState,
    projector: _Projector,
    state_service: object,
) -> ConversationRunCommandService:
    """构造只注入替身依赖的命令服务，并把会话工厂与 projector 装配替换为替身。"""

    monkeypatch.setattr(command_module, "main_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector", lambda: projector
    )
    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._command = SimpleNamespace(
        get=lambda *_args: None,
        create=lambda **_kwargs: SimpleNamespace(id=6, command_id="cmd-1"),
        get_by_run=lambda _run_id: SimpleNamespace(id=6, command_id="cmd-1"),
    )
    service._conversation_run = SimpleNamespace(
        create_run=lambda **_kwargs: SimpleNamespace(id=11, task_id=_TASK_ID, status="pending"),
        reset_run_for_edit=lambda *_args, **_kwargs: SimpleNamespace(
            id=11, task_id=_TASK_ID, status="pending"
        ),
    )
    service._context = SimpleNamespace(delete_by_run_id=lambda *_args, **_kwargs: None)
    service._run_state = run_state
    service._state = state_service
    service._task = SimpleNamespace(
        get_latest_run=lambda _task_id: SimpleNamespace(id=11, status="cancelled")
    )
    return service


_SNAPSHOT = {
    "runs": [],
    "current_run_id": None,
    "approvals": {},
    "context_usage_ratio": None,
    "context_usage_used": None,
    "context_window_total": None,
    "error": None,
}


def _snapshot_with_run(run_id: int = 11) -> dict[str, object]:
    """返回含指定 run 的合法快照。

    编排层在 ``claim_pending_run`` **之前**会复核该 run 在快照里可见（缺失即判定为快照分叉
    并自愈），因此命令服务用例的替身快照必须包含刚提交的 run。
    """

    return {
        **_SNAPSHOT,
        "current_run_id": run_id,
        "runs": [
            {
                "runId": run_id,
                "status": "pending",
                "endReason": None,
                "messages": [],
                "usage": None,
                "error": None,
            }
        ],
    }


def _state_service(
    snapshot: dict[str, object],
    *,
    fail_after_reads: int | None = None,
) -> SimpleNamespace:
    """构造满足当前契约的状态服务替身（``get_state`` / ``rebuild_state`` / ``publish_state``）。

    ``fail_after_reads`` 让第 N 次之后的 ``get_state`` 抛错，用于精确命中「认领完成后的快照
    重读失败」：第 1 次读是认领前的可见性复核，第 2 次读是取 ``initial_state``。
    """

    reads = {"count": 0}

    def _get_state(_task_id: int) -> dict[str, object]:
        reads["count"] += 1
        if fail_after_reads is not None and reads["count"] > fail_after_reads:
            raise RuntimeError("snapshot read failed")
        return snapshot

    return SimpleNamespace(
        get_state=_get_state,
        rebuild_state=lambda _task_id: snapshot,
        publish_state=lambda *_args: None,
    )


def test_start_or_attach_settles_run_when_projection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交后的 ``RunInitializedEvent`` 投影失败：当场收敛 run 为 cancelled 并原样抛出。"""

    run_state = _CommandServiceRunState()
    service = _build_command_service(
        monkeypatch,
        run_state=run_state,
        projector=_Projector(raises=True),
        state_service=_state_service(_snapshot_with_run()),
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        service.start_or_attach(
            command_id="cmd-1",
            command_type="new",
            payload_hash="hash",
            provider_id=None,
            model_name=None,
            task_id=_TASK_ID,
            run_command=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert run_state.settled == [(11, "run_setup_failed")]
    assert run_state.claimed == []


def test_start_or_attach_settles_run_when_claim_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``claim_pending_run`` 自身抛错（状态已提交、事件发布失败）：同样必须当场收敛。"""

    run_state = _CommandServiceRunState(claim_raises=True)
    service = _build_command_service(
        monkeypatch,
        run_state=run_state,
        projector=_Projector(),
        state_service=_state_service(_snapshot_with_run()),
    )

    with pytest.raises(RuntimeError, match="claim dispatch failed"):
        service.start_or_attach(
            command_id="cmd-1",
            command_type="new",
            payload_hash="hash",
            provider_id=None,
            model_name=None,
            task_id=_TASK_ID,
            run_command=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert run_state.settled == [(11, "run_setup_failed")]


def test_start_or_attach_settles_run_when_snapshot_reread_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """认领成功后快照重读失败：run 已是 running，仍必须收敛，不得留下无执行器的 active run。"""

    run_state = _CommandServiceRunState()
    service = _build_command_service(
        monkeypatch,
        run_state=run_state,
        projector=_Projector(),
        # 第 1 次读（认领前的可见性复核）成功，第 2 次读（认领后取 initial_state）失败。
        state_service=_state_service(_snapshot_with_run(), fail_after_reads=1),
    )

    with pytest.raises(RuntimeError, match="snapshot read failed"):
        service.start_or_attach(
            command_id="cmd-1",
            command_type="new",
            payload_hash="hash",
            provider_id=None,
            model_name=None,
            task_id=_TASK_ID,
            run_command=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert run_state.claimed == [11]
    assert run_state.settled == [(11, "run_setup_failed")]


def test_edit_or_restart_settles_run_when_post_commit_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编辑重跑提交后快照重读失败：与新建路径同一补偿语义，收敛为 cancelled 再抛出。"""

    run_state = _CommandServiceRunState()
    service = _build_command_service(
        monkeypatch,
        run_state=run_state,
        projector=_Projector(),
        # 编辑路径的读顺序与新建一致：先复核可见性，再在认领后取 initial_state。
        state_service=_state_service(_snapshot_with_run(), fail_after_reads=1),
    )

    with pytest.raises(RuntimeError, match="snapshot read failed"):
        service.edit_or_restart(
            command_id="cmd-2",
            command_type="edit",
            payload_hash="hash",
            task_id=_TASK_ID,
            run_id=11,
            provider_id=None,
            model_name=None,
            run_command=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert run_state.claimed == [11]
    assert run_state.settled == [(11, "run_setup_failed")]


def test_settle_converge_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """收敛写库自身失败只记日志：不得覆盖调用方原始异常。"""

    run_state = _CommandServiceRunState()

    def _cancel(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")

    run_state.cancel_run_if_running = _cancel  # type: ignore[method-assign]
    service = _build_command_service(
        monkeypatch,
        run_state=run_state,
        projector=_Projector(raises=True),
        state_service=_state_service(_snapshot_with_run()),
    )

    # 原始异常仍是投影失败，而不是收敛过程中的数据库异常
    with pytest.raises(RuntimeError, match="projection failed"):
        service.start_or_attach(
            command_id="cmd-1",
            command_type="new",
            payload_hash="hash",
            provider_id=None,
            model_name=None,
            task_id=_TASK_ID,
            run_command=SimpleNamespace(),  # type: ignore[arg-type]
        )


# ------------------------------------------------------------------ 分叉丢弃可观测性


def _capture_warnings() -> tuple[list[logging.LogRecord], logging.Handler]:
    # 注意：不能用「实例属性覆盖 emit」的写法——CPython 的 logging C 加速实现会绕过
    # 实例字典直接调用类型上的 emit，只有真正的 Handler 子类才会被调用。
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    log.addHandler(handler)
    return records, handler


def test_run_initialized_skeleton_drop_is_logged() -> None:
    """快照分叉导致骨架不建立时必须留 WARNING 日志，不再静默丢弃。"""

    diverged = {
        **_SNAPSHOT,
        "current_run_id": 12,
        "runs": [
            {
                "runId": 12,
                "status": "running",
                "endReason": None,
                "messages": [],
                "usage": None,
                "error": None,
            }
        ],
    }
    records, handler = _capture_warnings()
    try:
        assert RunInitializedEvent(task_id=_TASK_ID, run_id=11).plan(diverged) == []
    finally:
        log.removeHandler(handler)
    assert any(
        record.getMessage() == "transport_run_initialized_skeleton_dropped"
        and record.levelno == logging.WARNING
        for record in records
    )


def test_run_status_transition_drop_is_logged() -> None:
    """终态迁移被白名单丢弃时同样必须留痕（completed 不可逆分支）。"""

    from app.assistant_transport.event import RunStatusChangedEvent
    from app.models.enums.conversation_run_status import ConversationRunStatus

    settled = {
        **_SNAPSHOT,
        "current_run_id": 11,
        "runs": [
            {
                "runId": 11,
                "status": "completed",
                "endReason": None,
                "messages": [],
                "usage": None,
                "error": None,
            }
        ],
    }
    records, handler = _capture_warnings()
    try:
        assert (
            RunStatusChangedEvent(
                task_id=_TASK_ID, run_id=11, status=ConversationRunStatus.RUNNING
            ).plan(settled)
            == []
        )
    finally:
        log.removeHandler(handler)
    assert any(
        record.getMessage() == "transport_run_status_transition_dropped" for record in records
    )
