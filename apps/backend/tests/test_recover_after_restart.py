"""``RecoverService.recover_after_restart``：重启恢复各步按契约顺序执行、结果在步骤间传递。

各步自身的行为分别由 ``recover_orphaned_runs`` 相关测试与 ``test_restart_settlement.py`` 覆盖；
这里只验证编排。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.service import recover_service as recover_module
from app.service.recover_service import RecoverService


def test_recover_after_restart_runs_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    recovered = [SimpleNamespace(id=11)]
    dispatch = {3, 5}

    service = RecoverService.__new__(RecoverService)
    monkeypatch.setattr(
        service, "recover_orphaned_runs", lambda: calls.append(("runs", None)) or recovered
    )
    monkeypatch.setattr(
        "app.core.tool.multi_agent.agent_team.team_coordinator.get_agent_team_coordinator",
        lambda: SimpleNamespace(recover_after_restart=lambda: calls.append(("teams", None)) or [7]),
    )
    monkeypatch.setattr(
        service, "cancel_orphaned_team_nodes", lambda ids: calls.append(("nodes", ids)) or []
    )
    monkeypatch.setattr(
        recover_module.service_depends,
        "get_task_service",
        lambda: SimpleNamespace(list_task_ids_to_dispatch=lambda: dispatch),
    )
    monkeypatch.setattr(
        recover_module,
        "task_runtime_spaces",
        SimpleNamespace(get_or_create=lambda task_id: calls.append(("space", task_id))),
    )
    settlement = recover_module.restart_settlement
    reattached = frozenset({"call-a"})
    monkeypatch.setattr(
        settlement, "reattach_child_waits", lambda: calls.append(("reattach", None)) or reattached
    )
    monkeypatch.setattr(
        settlement,
        "interrupt_pending_placeholders",
        lambda exclude: calls.append(("interrupt", exclude)) or 0,
    )
    monkeypatch.setattr(
        settlement,
        "wake_suspended_terminal_runs",
        lambda task_ids: calls.append(("wake", task_ids)) or [],
    )

    service.recover_after_restart()

    steps = [name for name, _ in calls]
    assert steps[:3] == ["runs", "teams", "nodes"]
    assert sorted(arg for name, arg in calls if name == "space") == [3, 5]
    assert steps[-3:] == ["reattach", "interrupt", "wake"]
    # 作废 TeamRun 的节点先取消；先挂后中断，接管了的占位不被中断；
    # 终端唤醒与拉起 actor 用同一份候选。
    assert dict(calls)["nodes"] == [7]
    assert dict(calls)["interrupt"] is reattached
    assert dict(calls)["wake"] is dispatch


def test_only_suspended_runs_of_orphaned_team_nodes_are_cancelled(monkeypatch) -> None:
    """作废 TeamRun 的节点：挂起的取消（没人再等它），其余状态不动。"""
    from app.models.enums.conversation_run_status import RunTransition

    runs = {
        7: SimpleNamespace(id=70, status="suspended"),
        8: SimpleNamespace(id=80, status="completed"),
    }
    transitions: list[tuple[int, RunTransition, str]] = []
    service = RecoverService.__new__(RecoverService)
    service._ledger = SimpleNamespace(
        transition=lambda run_id, transition, *, end_reason: transitions.append(
            (run_id, transition, end_reason)
        ) or SimpleNamespace(id=run_id)
    )
    monkeypatch.setattr(
        recover_module.service_depends,
        "get_task_service",
        lambda: SimpleNamespace(require_current_run=lambda task_id: runs.get(task_id)),
    )

    assert service.cancel_orphaned_team_nodes([7, 8, 9]) == [70]
    assert transitions == [(70, RunTransition.CANCEL, "runtime_restarted")]
