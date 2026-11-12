"""``RecoverService.recover_after_restart``：重启恢复各步按契约顺序执行、结果在步骤间传递。

各步自身的行为分别由 ``recover_orphaned_runs`` 与 ``test_background_placeholders`` 覆盖；
这里只验证编排。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tool.multi_agent.agent_team.team_coordinator import RecoveredTeamRun
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
        "app.service.recover_service.service_depends.get_agent_team_coordinator",
        lambda: SimpleNamespace(
            recover_after_restart=lambda: calls.append(("teams", None))
            or [RecoveredTeamRun(parent_task_id=4, node_task_ids=[7])]
        ),
    )
    monkeypatch.setattr(
        service, "cancel_orphaned_team_nodes", lambda ids: calls.append(("nodes", ids)) or []
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
    assert steps[:3] == ["runs", "teams", "nodes"]
    assert sorted(arg for name, arg in calls if name == "space") == [3, 5]
    assert steps[-2:] == ["interrupt", "notify"]
    # 作废 TeamRun 的节点先取消；通知集合 = 被收敛的 Run 所属 Task ∪ 被收敛 Team 的主 Task（Team 不汇报）
    # ∪ 结算了占位的 Task。
    assert dict(calls)["nodes"] == [7]
    assert dict(calls)["notify"] == {1, 2, 4}


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


def test_restart_withdraws_only_the_interrupted_teams_unconsumed_node_input(
    backend_env, tmp_path, monkeypatch
):
    from app.core.workflow.message_queue.task_message import (
        AgentMessageBody,
        RunLaunch,
        TaskMessage,
    )
    from app.models.conversation_input import MessageSource
    from app.service import depends

    ws = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
    parent, node, other = [depends.get_workspace_service().create_task(ws.id, title)
                           for title in ("parent", "node", "child")]
    queue = depends.get_task_message_queue()
    for source in (parent, other):
        queue.put(node.id, TaskMessage.of(AgentMessageBody(
            source=MessageSource(task_id=source.id, name=source.title),
            launch=RunLaunch(model_config_id="test/m1"), text="queued",
        )))
    coordinator = depends.get_agent_team_coordinator()
    row = SimpleNamespace(id=17, parent_task_id=parent.id, state_json={
        "node_executions": {"review": {"node_id": "review", "task_id": node.id}}
    })
    monkeypatch.setattr(coordinator._team_run_crud, "list_active", lambda: [row])
    monkeypatch.setattr(coordinator, "_cancel_if_active", lambda *_: row)
    assert coordinator.recover_after_restart() == [
        RecoveredTeamRun(parent_task_id=parent.id, node_task_ids=[node.id])
    ]
    assert [message.body.source.task_id for message in queue.pending_nowait(node.id)] == [other.id]
