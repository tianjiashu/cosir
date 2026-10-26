"""结构化输出支线改写已完成 Run 的持久结果，不触碰生命周期。"""

from pathlib import Path

import pytest

from app.core.events import RunResultUpdatedEvent, RunStatusChangedEvent
from app.models.enums.conversation_run_status import RunTransition
from app.core import conversation_run_ledger as ledger_module
from app.service.depends import (
    get_conversation_run_crud,
    get_conversation_run_ledger,
    get_task_crud,
    get_workspace_crud,
)


@pytest.mark.parametrize("succeeds", [True, False])
def test_structured_result_only_updates_completed_fields(
    isolated_storage, monkeypatch: pytest.MonkeyPatch, succeeds: bool
) -> None:
    """完成事件只发一次；成功替换输出，失败保留原输出并写入支线原因。"""

    events = []
    monkeypatch.setattr(ledger_module, "dispatch_conversation_event", events.append)
    workspace = get_workspace_crud().create("structured-test", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "task")
    run_crud = get_conversation_run_crud()
    run = run_crud.create(task.id, status="running")
    state = get_conversation_run_ledger()

    state.transition(run.id, RunTransition.COMPLETE, final_output="自然回答", end_reason="stop")
    if succeeds:
        state.update_completed_result(run.id, final_output='{"ok":true}')
    else:
        state.update_completed_result(run.id, end_reason="structured_output_failed")

    stored = run_crud.get(run.id)
    assert stored.status == "completed"
    assert stored.final_output == ('{"ok":true}' if succeeds else "自然回答")
    assert stored.end_reason == ("stop" if succeeds else "structured_output_failed")
    assert [type(event) for event in events] == [RunStatusChangedEvent, RunResultUpdatedEvent]
    assert events[0].end_reason == "stop"

    other = run_crud.create(task.id, status="running")
    assert state.update_completed_result(other.id, end_reason="structured_output_failed") is None
    assert run_crud.get(other.id).end_reason is None
    assert len(events) == 2


def test_completion_persists_empty_output_and_finish_reason(
    isolated_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主线空正文也要与完成原因在同一次状态迁移中持久化并投影。"""

    events = []
    monkeypatch.setattr(ledger_module, "dispatch_conversation_event", events.append)
    workspace = get_workspace_crud().create("empty-output-test", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "task")
    run_crud = get_conversation_run_crud()
    run = run_crud.create(task.id, status="running")

    get_conversation_run_ledger().transition(
        run.id, RunTransition.COMPLETE, final_output="", end_reason="content_filter"
    )

    stored = run_crud.get(run.id)
    assert (stored.status, stored.final_output, stored.end_reason) == (
        "completed",
        "",
        "content_filter",
    )
    assert len(events) == 1
    assert events[0].end_reason == "content_filter"
