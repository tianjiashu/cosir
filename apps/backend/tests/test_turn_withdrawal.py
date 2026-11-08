"""编辑重跑撤回一轮：范围按回合号、保留按调用归属，以及这一轮全部副产物的收拢。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core.context.context_store import ContextStore
from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.background.background_job import BackgroundConclusion
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
from app.service import depends
from app.service.conversation_run import turn_withdrawal_service as withdrawal_module
from app.service.conversation_run.turn_withdrawal_service import plan_withdrawal
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from tests.test_agent_team_definitions import _configuration


def _user(text: str) -> UserMessageMetadata:
    return UserMessageMetadata(
        kind="user_input", presentation=UserInputPresentation(display_text=text)
    )


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
    """两轮对话：第一轮发起 ``call-early``，第二轮发起 ``call-late`` 与 Team 方案 ``call-team``。

    第一轮那次调用的结论、一条环境通知（与只针对这一轮的续写提示合成一行）都在第二轮途中并入。
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

    plan = plan_withdrawal(rows, from_turn=2)

    assert plan.calls == frozenset({"call-late", "call-team"})
    excluded = {r.sequence for r in rows if r.id in plan.excluded_row_ids}
    # 第一轮调用的结论（序号 8）回答的是第一轮，保留；其余第二轮的行全部移出。
    assert excluded == {4, 5, 6, 7, 9, 10}
    # 合成通知行整体移出：环境通知交给调用方重新投递，续写提示只针对这一轮，随之丢弃。
    assert [n.notice_kind for n in plan.redeliver] == ["system_prompt_delta"]


@pytest.fixture
def side_effects(history, monkeypatch):
    """被撤回那一轮留下的副产物，以及更早一轮的同类事物（必须不受影响）。"""
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
            tool_name="terminal_handoff",
            surface="terminal",
            resource_id="term",
            reason="password",
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
        TaskMessage.of(
            NoticeBody(content="stopped", notice_kind="user_cancelled"), delivery="next_run"
        ),
    )
    queued_input = queue.put(history.task_id, _input("排着的"))
    # 测试里没有 actor：新消息直接写进外部队列，与 ``task_runtime_spaces.enqueue`` 的持久化一致。
    monkeypatch.setattr(
        withdrawal_module,
        "task_runtime_spaces",
        SimpleNamespace(
            enqueue=lambda task_id, message, *, session: queue.put(
                task_id, message, session=session
            )
        ),
    )
    yield SimpleNamespace(revoked=revoked, team_id=team.id, queued_input=queued_input)
    background_registry.withdraw(history.task_id, "call-early")
    handoff_registry.drop_task(history.task_id)


def _input(text: str, delivery: str = "next_run") -> TaskMessage:
    return TaskMessage.of(
        UserInputBody(
            input_text=text,
            launch=RunLaunch(model_config_id=1),
            presentation=UserInputPresentation(display_text=text),
        ),
        delivery=delivery,  # type: ignore[arg-type]
    )


def _queued(task_id: int) -> list[tuple[str, object]]:
    def describe(message: TaskMessage) -> tuple[str, object]:
        body = message.body
        if isinstance(body, NoticeBody):
            return ("notice", body.notice_kind)
        if isinstance(body, BackgroundResultBody | DecisionBody):
            return (message.kind, body.tool_call_id)
        assert isinstance(body, UserInputBody)
        return (message.kind, body.input_text)

    return [describe(m) for m in depends.get_task_message_queue().pending_nowait(task_id)]


def test_withdraw_gathers_every_side_effect_of_the_turn(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(
        history.task_id, 12, _input("改过的", "immediate")
    )

    # 这一轮的后台作业被撤销，更早一轮的照常进行。
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
    # 队列：这一轮调用的结论、决定与「用户已停止」被删；更早调用的结论、排队的输入保留；环境通知
    # 重新投递，新消息入队。
    assert _queued(history.task_id) == [
        ("background_result", "call-early"),
        ("user_input", "排着的"),
        ("notice", "system_prompt_delta"),
        ("user_input", "改过的"),
    ]
    included = ConversationTaskContextCrud().get(history.task_id, include_in_context=True)
    assert [r.sequence for r in included] == [1, 2, 3, 8]


def test_withdraw_rejects_a_stale_target_before_touching_anything(history, side_effects) -> None:
    from app.core.task_actor import SessionConflictError

    with pytest.raises(SessionConflictError) as caught:
        depends.get_turn_withdrawal_service().withdraw(history.task_id, 11, _input("旧的"))

    assert caught.value.code == "EDIT_TARGET_STALE"
    assert side_effects.revoked == []
    assert len(ConversationTaskContextCrud().get(history.task_id, include_in_context=True)) == 10


def test_restart_sweep_does_not_settle_withdrawn_placeholders(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(
        history.task_id, 12, _input("改过的", "immediate")
    )

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
        "app.core.tool.background.backgraound_registry.deliver_background_result", slow_delivery
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
