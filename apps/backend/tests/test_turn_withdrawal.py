"""撤回回合：范围按回合号、保留按调用归属，以及这一回合全部副产物的收拢。"""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.context.context_store import ContextStore
from app.core.tool.background.background_job import BackgroundConclusion
from app.core.tool.background.background_registry import background_registry
from app.core.tool.handoff.handoff_registry import HandoffLease, handoff_registry
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, notices_to_context
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.core.workflow.message_queue.task_message import (
    BackgroundResultBody,
    DecisionBody,
    TaskMessage,
    UserInputBody,
)
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata, UserMessageMetadata
from app.models.withdrawal_plan import WithdrawalPlan
from app.service import depends
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from tests.test_agent_team_definitions import _configuration


def _user(text: str) -> UserMessageMetadata:
    return UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(text=text))


def _call(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}}
            for call_id in call_ids
        ],
    }


def _placeholder(call_id: str) -> tuple[dict, ToolMessageMetadata]:
    """仍在等结论的后台占位（metadata 带工作记录）。"""
    return (
        {"role": "tool", "tool_call_id": call_id, "content": "submitted"},
        ToolMessageMetadata(
            status="pending",
            background=BackgroundWork(
                run_id=1, kind="terminal_command", label="t", started_at="2026-01-01T00:00:00+00:00"
            ),
        ),
    )


def _result_of(call_id: str) -> UserMessageMetadata:
    return UserMessageMetadata(kind="background_result", tool_call_id=call_id)


@pytest.fixture
def history(isolated_storage, tmp_path):
    """两个回合：第一回合发起 ``call-early``，第二回合发起 ``call-late`` 与 Team 方案
    ``call-team``。

    第一回合那次调用的结论、一条 Task 级通知（与驱动内的续写提示合成一行）都在第二回合途中
    并入。
    """
    workspace = WorkspaceCrud().create("withdraw", str(tmp_path))
    task = TaskCrud().create(workspace.id, "withdraw", tool_schemas=[])
    run = ConversationRunCrud().create(task.id, status="completed")
    notice_message, notice_metadata = notices_to_context(
        [
            NoticeBody(content="workspace instructions changed", notice_kind="system_prompt_delta"),
            NoticeBody(content="continue", notice_kind="model_continuation"),
        ]
    )
    rows = [
        (1, {"role": "user", "content": "first"}, _user("first"), 11),
        (1, _call("call-early"), None, None),
        (1, *_placeholder("call-early"), None),
        (2, {"role": "user", "content": "second"}, _user("second"), 12),
        (2, _call("call-late", "call-team"), None, None),
        (2, *_placeholder("call-late"), None),
        (2, *_placeholder("call-team"), None),
        (2, {"role": "user", "content": "early done"}, _result_of("call-early"), None),
        (2, notice_message, notice_metadata, None),
        (2, {"role": "assistant", "content": "a2"}, None, None),
    ]
    store = ContextStore()
    for sequence, (turn, message, metadata, message_id) in enumerate(rows, start=1):
        store.append(
            task.id, run.id, message, sequence, metadata=metadata, message_id=message_id, turn=turn
        )
    return SimpleNamespace(task_id=task.id, run_id=run.id, workspace_id=workspace.id)


def test_plan_withdraws_the_turn_and_keeps_late_results_of_earlier_calls(history) -> None:
    rows = ConversationTaskContextCrud().get(history.task_id, include_in_context=False)

    plan = WithdrawalPlan.plan_withdrawal(rows, from_turn=2)

    assert plan.calls == frozenset({"call-late", "call-team"})
    excluded = {r.sequence for r in rows if r.id in plan.excluded_row_ids}
    # 第一回合调用的结论（序号 8）回答的是第一回合，保留；其余第二回合的行全部移出。
    assert excluded == {4, 5, 6, 7, 9, 10}
    # 合成通知行整体移出：Task 级通知交给调用方重新投递，回合级的续写提示随之丢弃。
    assert [n.notice_kind for n in plan.redeliver] == ["system_prompt_delta"]


@pytest.fixture
def side_effects(history, monkeypatch):
    """被撤回那一回合留下的副产物，以及更早回合的同类事物（必须不受影响）。"""
    # Team 协调器构造时读工具注册表；撤回只用它收敛待确认方案，不调度节点。
    monkeypatch.setattr("app.config.configuration.get_tool_registry", lambda: object())
    depends.get_agent_team_coordinator.cache_clear()
    revoked: list[str] = []
    for call_id in ("call-late", "call-early"):
        background_registry.register(
            call_id,
            task_id=history.task_id,
            run_id=history.run_id,
            kind="terminal_command",
            label="t",
            mode="background",
            on_cancel=lambda _reason, call_id=call_id: revoked.append(call_id),
        )
    handoff_registry.begin(
        HandoffLease(
            tool_call_id="call-late",
            task_id=history.task_id,
            tool_name="browser_handoff",
            surface="browser",
            resource_id="tab",
            reason="sign in",
        )
    )
    team = AgentTeamRunCrud().create(
        configuration=AgentTeamConfiguration.model_validate(_configuration()),
        workspace_id=history.workspace_id,
        parent_task_id=history.task_id,
        parent_run_id=history.run_id,
        tool_call_id="call-team",
        goal="审查",
        node_runtime_snapshots={
            "develop": {"agent_id": "general-assistant", "node_goal_summary": "开发"}
        },
    )
    queue = depends.get_task_message_queue()

    def result(call_id: str) -> TaskMessage:
        return TaskMessage.of(
            BackgroundResultBody(
                tool_call_id=call_id, tool_name="terminal_command", tool_result="{}", ended_at="t"
            )
        )

    queue.put(history.task_id, result("call-late"))
    queue.put(history.task_id, result("call-early"))
    queue.put(
        history.task_id,
        TaskMessage.of(
            DecisionBody(tool_call_id="call-team", tool_name="agent_team", decision="approve")
        ),
    )
    queue.put(
        history.task_id,
        TaskMessage.of(NoticeBody(content="shell exited", notice_kind="backend_restarted")),
    )
    queued_input = queue.put(history.task_id, _input("排着的"))
    yield SimpleNamespace(revoked=revoked, team_id=team.id, queued_input=queued_input)
    background_registry.withdraw(history.task_id, "call-early")
    handoff_registry.drop_task(history.task_id)


def _input(text: str) -> TaskMessage:
    return TaskMessage.of(
        UserInputBody(
            text=text,
            launch=RunLaunch(model_config_id="test/m1"),
            presentation=UserInputPresentation(text=text),
        ),
        delivery="next_run",
    )


def _queued(task_id: int) -> list[tuple[str, object]]:
    def describe(message: TaskMessage) -> tuple[str, object]:
        body = message.body
        if isinstance(body, NoticeBody):
            return ("notice", body.notice_kind)
        if isinstance(body, BackgroundResultBody | DecisionBody):
            return (message.kind, body.tool_call_id)
        assert isinstance(body, UserInputBody)
        return (message.kind, body.text)

    return [describe(m) for m in depends.get_task_message_queue().pending_nowait(task_id)]


def test_withdraw_gathers_every_side_effect_of_the_turn(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(history.task_id, 12)

    # 这一回合的后台作业被撤销，更早回合的照常进行。
    assert side_effects.revoked == ["call-late"]
    assert (
        background_registry.complete(
            history.task_id, "call-late", BackgroundConclusion({"ok": True})
        )
        is False
    )
    assert handoff_registry.leases(history.task_id) == []
    team = AgentTeamRunCrud().get_by_id(side_effects.team_id)
    assert team is not None and team.status == "cancelled"
    # 队列：这一回合调用的结论与决定被删；更早调用的结论、排队的通知与输入保留；移出的 Task 级
    # 通知重新投递。撤回不入队新消息、不开新 Run。
    assert _queued(history.task_id) == [
        ("background_result", "call-early"),
        ("notice", "backend_restarted"),
        ("user_input", "排着的"),
        ("notice", "system_prompt_delta"),
    ]
    included = ConversationTaskContextCrud().get(history.task_id, include_in_context=True)
    assert [r.sequence for r in included] == [1, 2, 3, 8]


def test_withdraw_rejects_a_stale_target_before_touching_anything(history, side_effects) -> None:
    from app.core.task_actor import SessionConflictError

    with pytest.raises(SessionConflictError) as caught:
        depends.get_turn_withdrawal_service().withdraw(history.task_id, 11)

    assert caught.value.code == "WITHDRAW_TARGET_STALE"
    assert side_effects.revoked == []
    assert len(ConversationTaskContextCrud().get(history.task_id, include_in_context=True)) == 10


def test_restart_sweep_does_not_settle_withdrawn_placeholders(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(history.task_id, 12)

    unsettled = ConversationTaskContextCrud().list_unsettled_placeholders(history.task_id)

    assert [r.message["tool_call_id"] for r in unsettled] == ["call-early"]


def test_withdraw_waits_for_a_conclusion_already_leaving_the_registry(monkeypatch) -> None:
    """结论已出登记表、还在锁外写队列时撤回：撤回要等它写完，调用方随后删得到这一行。"""
    task_id, call_id = 4242, "call-in-flight"
    background_registry.register(
        call_id,
        task_id=task_id,
        run_id=1,
        kind="terminal_command",
        label="t",
        mode="background",
        on_cancel=lambda _reason: None,
    )
    writing, release = threading.Event(), threading.Event()

    def slow_delivery(*_args, **_kwargs) -> bool:
        writing.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(
        "app.core.tool.background.background_registry.deliver_background_result", slow_delivery
    )
    delivering = threading.Thread(
        target=background_registry.complete,
        args=(task_id, call_id, BackgroundConclusion({"ok": True})),
    )
    delivering.start()
    assert writing.wait(5)
    withdrawn = threading.Event()
    withdrawer = threading.Thread(
        target=lambda: (background_registry.withdraw_calls(task_id, {call_id}), withdrawn.set())
    )
    withdrawer.start()
    assert not withdrawn.wait(0.2), "撤回不能先于在途投递返回"
    release.set()
    assert withdrawn.wait(5)
    delivering.join(5)
    withdrawer.join(5)


def test_the_runs_last_turn_counts_as_withdrawn_only_after_withdrawal(
    history, side_effects
) -> None:
    """恢复与「用户已停止」都据此判断：最后一个回合还在上下文里时不算撤回。"""
    store = ContextStore()
    assert store.run_last_turn_withdrawn(history.task_id, history.run_id) is False

    depends.get_turn_withdrawal_service().withdraw(history.task_id, 12)

    assert store.run_last_turn_withdrawn(history.task_id, history.run_id) is True


def test_fork_cannot_cut_at_a_withdrawn_turn(history, side_effects) -> None:
    """撤回留下了更早调用在这一回合才并入的结果（仍带回合号 2），fork 也不能切到这一回合。"""
    from app.service.task.fork_source import ForkSource

    depends.get_turn_withdrawal_service().withdraw(history.task_id, 12)
    rows = ConversationTaskContextCrud().get(history.task_id, include_in_context=False)
    source = ForkSource(task=depends.get_task_crud().get(history.task_id), rows=rows, runs=[])

    assert any(r.turn == 2 and r.retained for r in rows)
    with pytest.raises(KeyError):
        source.turn_start(2)
    assert source.turn_start(1).sequence == 1


def test_cold_rebuild_is_not_resumable_once_the_last_turn_is_withdrawn(
    history, side_effects
) -> None:
    """可恢复是后端事实：cancelled 的 Run 撤回最后一个回合后，冷重建给出的 ``resumable`` 为假。"""
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )

    runs = ConversationRunCrud()
    run = runs.get(history.run_id)
    task = TaskCrud().get(history.task_id)
    task.current_run_id = run.id
    cancelled = replace(run, status="cancelled")

    def resumable() -> bool:
        rows = ConversationTaskContextCrud().get(history.task_id, include_in_context=False)
        state = ConversationTaskStateRebuilder.rebuild(task, [cancelled], rows)
        return state["activity"]["resumable"]

    assert resumable() is True
    depends.get_turn_withdrawal_service().withdraw(history.task_id, 12)
    assert resumable() is False
