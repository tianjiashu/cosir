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
        "app.core.tool.handler.agent_team.coordinator.get_agent_team_coordinator",
        lambda: SimpleNamespace(recover_after_restart=lambda: calls.append(("teams", None)) or 0),
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
    monkeypatch.setattr(
        settlement,
        "interrupt_pending_placeholders",
        lambda: calls.append(("interrupt", None)) or 0,
    )
    monkeypatch.setattr(
        settlement,
        "settle_restarted_child_runs",
        lambda runs: calls.append(("settle", runs)) or [],
    )
    monkeypatch.setattr(
        settlement,
        "wake_suspended_terminal_runs",
        lambda task_ids: calls.append(("wake", task_ids)) or [],
    )

    service.recover_after_restart()

    steps = [name for name, _ in calls]
    assert steps[:2] == ["runs", "teams"]
    assert sorted(arg for name, arg in calls if name == "space") == [3, 5]
    assert steps[-3:] == ["interrupt", "settle", "wake"]
    # 子 Run 回投用收敛结果；终端唤醒与拉起 actor 用同一份候选。
    assert dict(calls)["settle"] is recovered
    assert dict(calls)["wake"] is dispatch
