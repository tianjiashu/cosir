"""快照分叉自愈回归测试。

对应不变量：**编排层必须保证「刚提交的 run」在 Transport 快照里可见**。快照是进程内
working copy，若它漏掉了某条 run 的终态事件，就会与 canonical 分叉，此时：

- 新 run 的 ``RunInitializedEvent`` 被陈旧事件防护丢弃（快照最后一个 run 仍非终态），
  随后 ``claim_pending_run`` 发布 RUNNING 时找不到该 run 而抛 ``KeyError``，表现为
  「该对话每次发送都 500」；
- ``resume`` 因快照 ``current_run_id`` 不匹配而永久返回「不是当前 run」。

进程内没有其它恢复入口，因此编排层在认领/校验之前显式复核并（必要时）按 canonical 重建。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.service import conversation_run_command_service as command_module
from app.assistant_transport.service import conversation_task_state_service as state_service_module
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandInput,
    ConversationRunCommandService,
)
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot

_TASK_ID = 7
_RUN_ID = 11


def _snapshot(
    *,
    runs: list[tuple[int, str]] | None = None,
    current_run_id: int | None = None,
) -> dict[str, object]:
    """构造带 user 消息的合法快照，``runs`` 为 ``(runId, status)`` 列表。"""

    return {
        "runs": [
            {
                "runId": run_id,
                "status": status,
                "endReason": None,
                "messages": [{"id": f"user-{run_id}", "role": "user", "parts": []}],
                "usage": None,
                "error": None,
            }
            for run_id, status in (runs or [])
        ],
        "current_run_id": current_run_id,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }


class _StateService:
    """记录快照读取、自愈重建与发布调用的替身状态服务。"""

    def __init__(self, *, current: dict[str, object], rebuilt: dict[str, object]) -> None:
        self._current = current
        self._rebuilt = rebuilt
        self.rebuild_calls: list[int] = []
        self.published: list[int] = []

    def get_state(self, _task_id: int) -> dict[str, object]:
        return self._current

    def rebuild_state(self, task_id: int) -> dict[str, object]:
        self.rebuild_calls.append(task_id)
        self._current = self._rebuilt
        return self._rebuilt

    def publish_state(self, task_id: int, _state: object) -> None:
        self.published.append(task_id)


class _RunState:
    """记录认领/续跑/收敛调用的替身 run 状态服务。"""

    def __init__(self) -> None:
        self.claimed: list[int] = []
        self.resumed: list[int] = []
        self.settled: list[tuple[int, str]] = []

    def has_active_run(self, _task_id: int, session: object = None) -> bool:
        return False

    def claim_pending_run(self, run_id: int) -> SimpleNamespace:
        self.claimed.append(run_id)
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status="running")

    def resume_cancelled_run(self, run_id: int) -> SimpleNamespace:
        self.resumed.append(run_id)
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status="running")

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "user_cancelled", **_kwargs: Any
    ) -> SimpleNamespace:
        self.settled.append((run_id, end_reason))
        return SimpleNamespace(id=run_id, task_id=_TASK_ID, status="cancelled")


class _Projector:
    """记录投影事件类型的替身 projector。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    def process(self, event: object, **_kwargs: Any) -> None:
        self.events.append(event)


class _FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeSessionFactory:
    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()


def _build_command_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_state: _RunState,
    state_service: _StateService,
) -> ConversationRunCommandService:
    """构造只注入替身依赖的命令服务（会话工厂与 projector 一并替换）。"""

    monkeypatch.setattr(command_module, "main_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(
        "app.service.depends.get_conversation_event_projector", lambda: _Projector()
    )
    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._command = SimpleNamespace(
        get=lambda *_args: None,
        create=lambda **_kwargs: SimpleNamespace(id=6, command_id="cmd-1"),
        get_by_run=lambda _run_id: SimpleNamespace(id=6, command_id="cmd-1"),
    )
    service._conversation_run = SimpleNamespace(
        create_run=lambda **_kwargs: SimpleNamespace(
            id=_RUN_ID, task_id=_TASK_ID, status="pending"
        ),
        reset_run_for_edit=lambda *_args, **_kwargs: SimpleNamespace(
            id=_RUN_ID, task_id=_TASK_ID, status="pending"
        ),
    )
    service._context = SimpleNamespace(delete_by_run_id=lambda *_args, **_kwargs: None)
    service._run_state = run_state
    service._state = state_service
    service._task = SimpleNamespace(
        get_latest_run=lambda _task_id: SimpleNamespace(id=_RUN_ID, status="cancelled")
    )
    return service


def _start_or_attach(service: ConversationRunCommandService) -> object:
    return service.start_or_attach(
        commands=[ConversationRunCommandInput(command_id="cmd-1", command_type="new")],
        payload_hash="hash",
        provider_id=None,
        model_name=None,
        task_id=_TASK_ID,
        run_command=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _edit_or_restart(service: ConversationRunCommandService) -> object:
    return service.edit_or_restart(
        commands=[ConversationRunCommandInput(command_id="cmd-2", command_type="edit")],
        payload_hash="hash",
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        provider_id=None,
        model_name=None,
        run_command=SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_start_or_attach_rebuilds_diverged_snapshot_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照缺新 run（分叉）时：先按 canonical 重建并发布 full 帧，再认领，不再 500。"""

    run_state = _RunState()
    state_service = _StateService(
        current=_snapshot(runs=[(10, "running")], current_run_id=10),
        rebuilt=_snapshot(runs=[(10, "cancelled"), (_RUN_ID, "pending")], current_run_id=_RUN_ID),
    )
    service = _build_command_service(
        monkeypatch, run_state=run_state, state_service=state_service
    )

    result = _start_or_attach(service)

    assert state_service.rebuild_calls == [_TASK_ID]
    assert state_service.published == [_TASK_ID]
    assert run_state.claimed == [_RUN_ID]
    assert run_state.settled == []
    assert result.created is True  # type: ignore[attr-defined]


def test_edit_or_restart_rebuilds_diverged_snapshot_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编辑重跑同样在认领前自愈；自愈帧之外仍保留该路径收尾的 full 帧。"""

    run_state = _RunState()
    state_service = _StateService(
        current=_snapshot(runs=[(10, "running")], current_run_id=10),
        rebuilt=_snapshot(runs=[(10, "cancelled"), (_RUN_ID, "pending")], current_run_id=_RUN_ID),
    )
    service = _build_command_service(
        monkeypatch, run_state=run_state, state_service=state_service
    )

    result = _edit_or_restart(service)

    assert state_service.rebuild_calls == [_TASK_ID]
    # 自愈发布一次 full 帧，编辑路径收尾再发布一次（既有行为）。
    assert state_service.published == [_TASK_ID, _TASK_ID]
    assert run_state.claimed == [_RUN_ID]
    assert result.mode == "edit"  # type: ignore[attr-defined]


def test_resume_latest_run_rebuilds_diverged_snapshot_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照缺该 run 时 resume 先自愈再校验归属，否则会永久返回「不是当前 run」。"""

    run_state = _RunState()
    state_service = _StateService(
        current=_snapshot(runs=[(10, "completed")], current_run_id=10),
        rebuilt=_snapshot(
            runs=[(10, "completed"), (_RUN_ID, "cancelled")], current_run_id=_RUN_ID
        ),
    )
    service = _build_command_service(
        monkeypatch, run_state=run_state, state_service=state_service
    )

    result = service.resume_latest_run(task_id=_TASK_ID, run_id=_RUN_ID)

    assert state_service.rebuild_calls == [_TASK_ID]
    assert run_state.resumed == [_RUN_ID]
    assert result.execution_mode == "resume"
    assert result.mode == "resume"


def test_ensure_run_visible_fails_fast_when_run_absent_after_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建后快照仍无该 run：立即失败且**不**认领，同时仍收敛已提交的 run。"""

    run_state = _RunState()
    state_service = _StateService(
        current=_snapshot(runs=[(10, "running")], current_run_id=10),
        rebuilt=_snapshot(runs=[(10, "running")], current_run_id=10),
    )
    service = _build_command_service(
        monkeypatch, run_state=run_state, state_service=state_service
    )

    with pytest.raises(RuntimeError, match="absent from both snapshot and canonical rebuild"):
        _start_or_attach(service)

    assert run_state.claimed == []
    assert run_state.settled == [(_RUN_ID, "run_setup_failed")]


def test_start_or_attach_skips_rebuild_when_skeleton_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常路径只做只读复核：不重建、不额外发布 full 帧。"""

    run_state = _RunState()
    state_service = _StateService(
        current=_snapshot(
            runs=[(10, "completed"), (_RUN_ID, "pending")], current_run_id=_RUN_ID
        ),
        rebuilt=_snapshot(
            runs=[(10, "completed"), (_RUN_ID, "pending")], current_run_id=_RUN_ID
        ),
    )
    service = _build_command_service(
        monkeypatch, run_state=run_state, state_service=state_service
    )

    _start_or_attach(service)

    assert state_service.rebuild_calls == []
    assert state_service.published == []
    assert run_state.claimed == [_RUN_ID]


def test_rebuild_state_unloads_working_copy_before_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rebuild_state`` 必须先卸载进程内副本再按 canonical 重建，而不是复用陈旧副本。"""

    events: list[str] = []
    rebuilt: ConversationStateSnapshot = _snapshot()  # type: ignore[assignment]

    class _Space:
        def unload_snapshot(self) -> None:
            events.append("unload")

        def get_snapshot(
            self, loader: Callable[[], ConversationStateSnapshot]
        ) -> ConversationStateSnapshot:
            state = loader()
            events.append("load")
            return state

    monkeypatch.setattr(
        state_service_module,
        "task_runtime_spaces",
        SimpleNamespace(get_or_create=lambda _task_id: _Space()),
    )

    def _loader(_task_id: int) -> ConversationStateSnapshot:
        events.append("rebuild")
        return rebuilt

    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    service._rebuild = _loader  # type: ignore[method-assign]

    assert service.rebuild_state(_TASK_ID) == rebuilt
    assert events == ["unload", "rebuild", "load"]
