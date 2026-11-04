"""重启收尾：委派等待按占位重新挂上；挂起在终端上的 Run 被唤醒；其余 Task 首次驱动时
得到一次旧终端失效提醒。

只依赖 Run 状态与 context 里的工具调用事实，协作者全用替身。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.tool.background import restart_settlement
from app.core.tool.multi_agent.child_agent import child_run_job
from app.core.workflow.message_queue.task_message import (
    BackgroundResultBody,
    NoticeBody,
    TaskMessage,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata
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
            get_task_message_queue=lambda: SimpleNamespace(pending_nowait=lambda _: []),
        ),
    )
    monkeypatch.setattr(
        restart_settlement,
        "_terminal_tool_names",
        lambda *_groups: frozenset({"terminal_start", "terminal_write", "terminal_read"}),
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
    assert (task_id, tool_name) == (1, "backend_restart")
    assert result["end_reason"] == "backend_restarted"
    assert restart_env.spaces[1].terminals_invalidation_announced is True


def test_woken_task_does_not_get_a_second_notice_on_first_drive(restart_env) -> None:
    restart_settlement.wake_suspended_terminal_runs(restart_env.task_ids)
    assert restart_settlement.first_drive_notice(restart_env.spaces[1]) is None


def test_first_drive_notice_once_per_process(restart_env) -> None:
    space = SimpleNamespace(task_id=3, terminals_invalidation_announced=False)
    notice = restart_settlement.first_drive_notice(space)
    assert isinstance(notice, NoticeBody)
    assert notice.notice_kind == "terminals_invalidated"
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


def _placeholder(task_id: int, run_id: int, tool_call_id: str, target: int | None):
    return ConversationTaskContextRecord(
        task_id=task_id,
        run_id=run_id,
        message={"role": "tool", "tool_call_id": tool_call_id, "content": "submitted"},
        metadata=ToolMessageMetadata(
            status="pending",
            background=BackgroundWork(
                kind="child_agent",
                label="child",
                started_at="2026-11-01T00:00:00+00:00",
                target_task_id=target,
            ),
        ),
        include_in_context=True,
        sequence=1,
    )


@pytest.fixture
def reattach_env(monkeypatch: pytest.MonkeyPatch):
    """父 Task 的当前 Run、目标子 Task、父队列与重新挂等待全用替身。"""
    parents = {1: "suspended", 4: "cancelled", 6: "completed", 7: "failed", 8: "suspended"}
    existing_tasks = {2, 5}
    queued = {
        6: [
            TaskMessage.of(
                BackgroundResultBody(
                    tool_call_id="call-c", tool_name="child_agent", tool_result="{}",
                    ended_at="2026-11-01T00:01:00+00:00",
                ),
                message_id=60,
            )
        ]
    }
    placeholders_ = [
        _placeholder(1, 10, "call-a", 2),  # 父在等、子还在：重新挂上
        _placeholder(4, 40, "call-b", 5),  # 父被重启打断：不接管
        _placeholder(6, 60, "call-c", 5),  # 结论已入队：不再挂，但也不中断
        _placeholder(7, 70, "call-d", None),  # 旧行没记目标：投 unavailable 唤醒父 Agent
        _placeholder(8, 80, "call-e", 99),  # 目标已删除：同上
    ]

    def get_task(task_id: int):
        if task_id not in existing_tasks:
            raise KeyError(task_id)
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(
        restart_settlement,
        "service_depends",
        SimpleNamespace(
            get_conversation_task_context_service=lambda: SimpleNamespace(
                pending_background_placeholders=lambda: placeholders_
            ),
            get_task_service=lambda: SimpleNamespace(
                get_task=get_task,
                require_current_run=lambda task_id: SimpleNamespace(id=10*task_id, status=parents[task_id]),
            ),
            get_task_message_queue=lambda: SimpleNamespace(
                pending_nowait=lambda task_id: queued.get(task_id, [])
            ),
        ),
    )
    delivered: list[tuple[int, str | None, dict[str, object]]] = []
    reattached: list[tuple[int, int | None, str, int | None]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result, wake_run_id) -> bool:
        delivered.append((task_id, tool_call_id, result))
        return True

    def reattach(*, task_id, run_id, tool_call_id, work, loop) -> None:
        reattached.append((task_id, run_id, tool_call_id, work.target_task_id))

    monkeypatch.setattr(restart_settlement, "deliver_background_result", deliver)
    monkeypatch.setattr(child_run_job, "reattach_child_wait", reattach)
    return SimpleNamespace(delivered=delivered, reattached=reattached)


async def test_child_waits_are_reattached_from_persisted_placeholders(reattach_env) -> None:
    """重启后按占位重新挂上委派等待；接管了的（含已入队、已投 unavailable）都不再中断。"""
    handled = restart_settlement.restore_background_waits()

    assert handled == {"call-a", "call-c", "call-e"}
    assert reattach_env.reattached == [(1, 10, "call-a", 2)]
    # 目标缺失时按原调用投结论把父 Agent 唤醒，不让它永远挂着。
    assert [(task_id, call) for task_id, call, _ in reattach_env.delivered] == [
        (8, "call-e"),
    ]
    assert all(result["end_reason"] == "unavailable" for _, _, result in reattach_env.delivered)


def test_interrupt_leaves_reattached_placeholders_alone(monkeypatch) -> None:
    seen: list[frozenset[str]] = []

    def settle(task_id, tool_call_ids, status, hint, *, exclude):
        seen.append(exclude)
        return 3

    monkeypatch.setattr(restart_settlement.placeholders, "settle_placeholders", settle)
    assert restart_settlement.interrupt_pending_placeholders(exclude=frozenset({"call-a"})) == 3
    assert seen == [frozenset({"call-a"})]
