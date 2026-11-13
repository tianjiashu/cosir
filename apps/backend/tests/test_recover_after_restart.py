"""``RecoverService.recover_after_restart``：重启恢复各步按契约顺序执行、结果在步骤间传递。

各步自身的行为分别由 ``recover_orphaned_runs`` 与 ``test_background_placeholders`` 覆盖；
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
    recovered = [SimpleNamespace(id=11, task_id=1)]
    dispatch = {3, 5}

    service = RecoverService.__new__(RecoverService)
    monkeypatch.setattr(
        service, "recover_orphaned_runs", lambda: calls.append(("runs", None)) or recovered
    )
    monkeypatch.setattr(
        recover_module.service_depends,
        "get_task_service",
        lambda: SimpleNamespace(
            list_task_ids_to_dispatch=lambda: dispatch,
            get_task=lambda task_id: SimpleNamespace(id=task_id, deriver_id=lambda: None),
        ),
    )
    monkeypatch.setattr(
        recover_module,
        "task_runtime_spaces",
        SimpleNamespace(get_or_create=lambda task_id: calls.append(("space", task_id))),
    )
    monkeypatch.setattr(
        service,
        "interrupt_placeholders",
        lambda task_ids: calls.append(("interrupt", task_ids)) or frozenset({2}),
    )
    monkeypatch.setattr(
        service,
        "notify_interrupted",
        lambda task_ids: calls.append(("notify", frozenset(task_ids))),
    )
    service.recover_after_restart()

    steps = [name for name, _ in calls]
    assert steps[0] == "runs"
    assert sorted(arg for name, arg in calls if name == "space") == [3, 5]
    assert steps[-2:] == ["interrupt", "notify"]
    # 通知集合 = 被收敛的 Run 所属 Task ∪ 它们的派生方（这里没有）∪ 结算了占位的 Task。
    assert dict(calls)["notify"] == {1, 2}
