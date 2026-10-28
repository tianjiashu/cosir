"""重启收尾：挂起在终端上的 Run 被唤醒，其余 Task 首次驱动时得到一次旧终端失效提醒。

只依赖 Run 状态与 context 里的工具调用事实，协作者全用替身。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.workflow.deferred import restart_settlement
from app.core.workflow.inbox.task_message import NoticeBody
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.service import depends


def _assistant_call(task_id: int, run_id: int, sequence: int, tool_name: str):
    return ConversationTaskContextRecord(
        task_id=task_id,
        run_id=run_id,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        include_in_context=True,
        sequence=sequence,
    )


class _Contexts:
    def __init__(self, calls: dict[int, list[tuple[int, str]]]) -> None:
        self._calls = calls

    def has_tool_call(
        self, task_id: int, tool_names: frozenset[str], *, run_id: int | None = None
    ) -> bool:
        return any(
            name in tool_names and (run_id is None or owner == run_id)
            for owner, name in self._calls.get(task_id, [])
        )


@pytest.fixture
def restart_env(monkeypatch: pytest.MonkeyPatch):
    """重启唤醒的协作者全用替身：Task 当前 Run、context 调用事实、space 与投递。"""
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    runs = {
        1: SimpleNamespace(id=11, status="suspended"),  # 挂起且本 Run 调用过终端
        2: SimpleNamespace(id=21, status="suspended"),  # 挂起但终端调用在旧 Run
        3: SimpleNamespace(id=31, status="completed"),  # 已结束：交给首次驱动提醒
        4: None,
    }
    calls = {
        1: [(11, "terminal_write")],
        2: [(20, "terminal_start"), (21, "wait_messages")],
        3: [(31, "terminal_start")],
    }
    spaces: dict[int, SimpleNamespace] = {}
    delivered: list[tuple[int, str, dict[str, object]]] = []
    tasks = SimpleNamespace(require_current_run=lambda task_id: runs[task_id])
    monkeypatch.setattr(
        restart_settlement,
        "service_depends",
        SimpleNamespace(
            get_task_service=lambda: tasks,
            get_conversation_task_context_service=lambda: _Contexts(calls),
        ),
    )
    monkeypatch.setattr(
        restart_settlement,
        "_terminal_tool_names",
        lambda: frozenset({"terminal_start", "terminal_write", "terminal_read"}),
    )
    monkeypatch.setattr(
        task_runtime_spaces,
        "get_or_create",
        lambda task_id: spaces.setdefault(
            task_id,
            SimpleNamespace(task_id=task_id, terminals_invalidation_announced=False),
        ),
    )

    def deliver(
        task_id: int, *, tool_call_id: str | None, tool_name: str, result: dict[str, object]
    ) -> bool:
        assert tool_call_id is None  # 重启结论不对应某次调用
        delivered.append((task_id, tool_name, result))
        return True

    monkeypatch.setattr(restart_settlement, "deliver_background_result", deliver)
    return SimpleNamespace(spaces=spaces, delivered=delivered, task_ids=set(runs))


def test_restart_wakes_only_runs_suspended_on_terminals(restart_env) -> None:
    assert restart_settlement.wake_suspended_terminal_runs(restart_env.task_ids) == [1]
    [(task_id, tool_name, result)] = restart_env.delivered
    assert (task_id, tool_name) == (1, "terminal_wait")
    assert result["end_reason"] == "backend_restarted"
    assert restart_env.spaces[1].terminals_invalidation_announced is True


def test_woken_task_does_not_get_a_second_notice_on_first_drive(restart_env) -> None:
    restart_settlement.wake_suspended_terminal_runs(restart_env.task_ids)
    assert restart_settlement.first_drive_notice(restart_env.spaces[1]) is None


def test_first_drive_notice_once_per_process(restart_env) -> None:
    space = SimpleNamespace(task_id=3, terminals_invalidation_announced=False)
    message = restart_settlement.first_drive_notice(space)
    assert message is not None
    assert isinstance(message.body, NoticeBody)
    assert message.body.notice_kind == "terminals_invalidated"
    assert message.delivery == "immediate"
    assert restart_settlement.first_drive_notice(space) is None


def test_first_drive_notice_covers_any_terminal_tool(restart_env) -> None:
    """只操作过用户新建的终端（没有 terminal_start）也要提醒：判定口径与重启唤醒一致。"""
    space = SimpleNamespace(task_id=1, terminals_invalidation_announced=False)
    assert restart_settlement.first_drive_notice(space) is not None


def test_first_drive_without_terminal_calls_marks_announced(restart_env) -> None:
    space = SimpleNamespace(task_id=4, terminals_invalidation_announced=False)
    assert restart_settlement.first_drive_notice(space) is None
    assert space.terminals_invalidation_announced is True


@pytest.mark.usefixtures("isolated_storage")
def test_has_tool_call_reads_in_context_assistant_calls(tmp_path: Path) -> None:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "task")
    runs = depends.get_conversation_run_crud()
    first = runs.create(task.id, "completed").id
    second = runs.create(task.id, "completed").id
    crud = depends.get_conversation_task_context_crud()
    crud.create(_assistant_call(task.id, first, 1, "terminal_start"))
    crud.create(_assistant_call(task.id, second, 2, "read_file"))
    service = depends.get_conversation_task_context_service()
    names = frozenset({"terminal_start"})
    assert service.has_tool_call(task.id, names)
    assert service.has_tool_call(task.id, names, run_id=first)
    assert not service.has_tool_call(task.id, names, run_id=second)
    assert not service.has_tool_call(task.id, frozenset({"terminal_close"}))


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch):
    """子 Task 2 委派自父 Task 1，子 Task 5 委派自父 Task 4；Task 8 不是委派子 Task。"""
    parents = {1: "suspended", 4: "cancelled", 8: "completed"}
    tasks = {
        2: SimpleNamespace(task_type="delegate_task", parent_task_id=1),
        5: SimpleNamespace(task_type="delegate_task", parent_task_id=4),
        9: SimpleNamespace(task_type="agent_team_node", parent_task_id=8),
    }
    delivered: list[tuple[int, str | None, str, dict[str, object]]] = []
    monkeypatch.setattr(
        restart_settlement,
        "service_depends",
        SimpleNamespace(
            get_task_service=lambda: SimpleNamespace(
                get_task=lambda task_id: tasks[task_id],
                require_current_run=lambda task_id: SimpleNamespace(status=parents[task_id]),
            )
        ),
    )

    def deliver(task_id, *, tool_call_id, tool_name, result) -> bool:
        delivered.append((task_id, tool_call_id, tool_name, result))
        return True

    monkeypatch.setattr(restart_settlement, "deliver_background_result", deliver)
    return delivered


def _recovered(run_id: int, task_id: int, trigger: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        task_id=task_id,
        status="cancelled",
        end_reason="runtime_restarted",
        final_output=None,
        trigger_tool_call_id=trigger,
    )


def test_restarted_child_run_is_reported_to_its_waiting_parent(child_env) -> None:
    settled = restart_settlement.settle_restarted_child_runs(
        [
            _recovered(20, 2, "call-delegate"),  # 父挂起在等：回投
            _recovered(50, 5, "call-other"),  # 父自己也被重启打断：不回投
            _recovered(90, 9, "call-node"),  # 不是委派子 Task
            _recovered(21, 2, None),  # 没有触发调用
        ]
    )
    assert settled == [20]
    [(task_id, tool_call_id, tool_name, result)] = child_env
    assert (task_id, tool_call_id, tool_name) == (1, "call-delegate", "child_agent")
    assert result == {
        "child_task_id": 2,
        "status": "cancelled",
        "end_reason": "runtime_restarted",
        "final_output": None,
    }


def test_no_triggered_runs_touches_nothing(child_env, monkeypatch) -> None:
    monkeypatch.setattr(restart_settlement, "service_depends", None)
    assert restart_settlement.settle_restarted_child_runs([_recovered(1, 2, None)]) == []
