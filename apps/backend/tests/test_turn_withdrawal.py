"""撤回回合：范围按回合号、保留按调用归属，以及这一回合全部副产物的收拢。"""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.core.context.context_store import ContextStore
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, notices_to_context
from app.core.workflow.message_queue.message_bodies.run_launch import RunLaunch
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    DecisionBody,
    SessionMessage,
    UserInputBody,
)
from app.models.message_context.message_metadata.tool_message_metadata import (
    BackgroundWork,
    ToolMessageMetadata,
)
from app.models.message_context.message_metadata.user_message_metadata import (
    UserInputPresentation,
    UserMessageMetadata,
)
from app.service import depends
from app.service.conversation.turn_withdrawal_plan import WithdrawalPlan
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.tools.tool_handler.terminal.command_job import CommandConclusion
from app.tools.tool_handler.terminal.command_results import CommandResultRegistry, command_results
from tests.command_job_fakes import FakeCommandJob


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
                run_id=1,
                label="t",
                started_at="2026-01-01T00:00:00+00:00",
            ),
        ),
    )


def _result_of(call_id: str) -> UserMessageMetadata:
    return UserMessageMetadata(kind="background_result", tool_call_id=call_id)


@pytest.fixture
def history(isolated_storage, tmp_path):
    """两个回合：第一回合发起 ``call-early``，第二回合发起 ``call-late`` 与等用户决定的
    ``call-decide``。

    第一回合那次调用的结论、一条 Session 级通知（与驱动内的续写提示合成一行）都在第二回合途中
    并入。
    """
    workspace = WorkspaceCrud().create("withdraw", str(tmp_path))
    session = SessionCrud().create(workspace.id, "withdraw")
    run = RunCrud().create(session.id, status="completed")
    notice_message, notice_metadata = notices_to_context(
        [
            NoticeBody(content="workspace instructions changed", notice_kind="fork_handover"),
            NoticeBody(content="continue", notice_kind="model_continuation"),
        ]
    )
    rows = [
        (1, {"role": "user", "content": "first"}, _user("first"), 11),
        (1, _call("call-early"), None, None),
        (1, *_placeholder("call-early"), None),
        (2, {"role": "user", "content": "second"}, _user("second"), 12),
        (2, _call("call-late", "call-decide"), None, None),
        (2, *_placeholder("call-late"), None),
        (2, *_placeholder("call-decide"), None),
        (2, {"role": "user", "content": "early done"}, _result_of("call-early"), None),
        (2, notice_message, notice_metadata, None),
        (2, {"role": "assistant", "content": "a2"}, None, None),
    ]
    store = ContextStore()
    for sequence, (turn, message, metadata, message_id) in enumerate(rows, start=1):
        store.append(
            session.id,
            run.id,
            message,
            sequence,
            metadata=metadata,
            message_id=message_id,
            turn=turn,
        )
    return SimpleNamespace(session_id=session.id, run_id=run.id, workspace_id=workspace.id)


def test_plan_withdraws_the_turn_and_keeps_late_results_of_earlier_calls(history) -> None:
    rows = SessionContextCrud().get(history.session_id, include_in_context=False)

    plan = WithdrawalPlan.plan_withdrawal(rows, from_turn=2)

    assert plan.calls == frozenset({"call-late", "call-decide"})
    excluded = {r.sequence for r in rows if r.id in plan.excluded_row_ids}
    # 第一回合调用的结论（序号 8）回答的是第一回合，保留；其余第二回合的行全部移出。
    assert excluded == {4, 5, 6, 7, 9, 10}
    # 合成通知行整体移出：Session 级通知交给调用方重新投递，回合级的续写提示随之丢弃。
    assert [n.notice_kind for n in plan.redeliver] == ["fork_handover"]


@pytest.fixture
def side_effects(history):
    """被撤回那一回合留下的副产物，以及更早回合的同类事物（必须不受影响）。"""
    revoked: list[str] = []
    for call_id in ("call-late", "call-early"):
        command_results.register(
            call_id,
            session_id=history.session_id,
            run_id=history.run_id,
            label="t",
            job=FakeCommandJob(lambda call_id=call_id: revoked.append(call_id)),
        )
    queue = depends.get_session_message_queue()

    def result(call_id: str) -> SessionMessage:
        return SessionMessage.of(
            BackgroundResultBody(
                tool_call_id=call_id, tool_name="terminal_command", tool_result="{}", ended_at="t"
            )
        )

    queue.put(history.session_id, result("call-late"))
    queue.put(history.session_id, result("call-early"))
    queue.put(
        history.session_id,
        SessionMessage.of(
            DecisionBody(tool_call_id="call-decide", tool_name="ask_user", decision="approve")
        ),
    )
    queue.put(
        history.session_id,
        SessionMessage.of(NoticeBody(content="shell exited", notice_kind="backend_restarted")),
    )
    queued_input = queue.put(history.session_id, _input("排着的"))
    yield SimpleNamespace(revoked=revoked, queued_input=queued_input)
    command_results.withdraw_calls(history.session_id, ["call-early"])


def _input(text: str) -> SessionMessage:
    return SessionMessage.of(
        UserInputBody(
            text=text,
            launch=RunLaunch(model_config_id="test/m1"),
            presentation=UserInputPresentation(text=text),
        ),
        delivery="next_run",
    )


def _queued(session_id: int) -> list[tuple[str, object]]:
    def describe(message: SessionMessage) -> tuple[str, object]:
        body = message.body
        if isinstance(body, NoticeBody):
            return ("notice", body.notice_kind)
        if isinstance(body, BackgroundResultBody | DecisionBody):
            return (message.kind, body.tool_call_id)
        assert isinstance(body, UserInputBody)
        return (message.kind, body.text)

    return [describe(m) for m in depends.get_session_message_queue().pending_nowait(session_id)]


def test_withdraw_gathers_every_side_effect_of_the_turn(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)

    # 这一回合的后台作业被撤销，更早回合的照常进行。
    assert side_effects.revoked == ["call-late"]
    assert (
        command_results.complete(history.session_id, "call-late", CommandConclusion({"ok": True}))
        is False
    )
    # 队列：这一回合调用的结论与决定被删；更早调用的结论、排队的通知与输入保留；移出的 Session 级
    # 通知重新投递。撤回不入队新消息、不开新 Run。
    assert _queued(history.session_id) == [
        ("background_result", "call-early"),
        ("notice", "backend_restarted"),
        ("user_input", "排着的"),
        ("notice", "fork_handover"),
    ]
    included = SessionContextCrud().get(history.session_id, include_in_context=True)
    assert [r.sequence for r in included] == [1, 2, 3, 8]


def test_withdraw_rejects_a_stale_target_before_touching_anything(history, side_effects) -> None:
    from app.models.session.session_command_errors import SessionCommandConflictError

    with pytest.raises(SessionCommandConflictError) as caught:
        depends.get_turn_withdrawal_service().withdraw(history.session_id, 11)

    assert caught.value.code == "WITHDRAW_TARGET_STALE"
    assert side_effects.revoked == []
    assert len(SessionContextCrud().get(history.session_id, include_in_context=True)) == 10


def test_restart_sweep_does_not_settle_withdrawn_placeholders(history, side_effects) -> None:
    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)

    unsettled = SessionContextCrud().list_unsettled_placeholders(history.session_id)

    assert [r.message["tool_call_id"] for r in unsettled] == ["call-early"]


def test_withdraw_waits_for_a_conclusion_already_leaving_the_registry(monkeypatch) -> None:
    """结论已出登记表、还在锁外写队列时撤回：撤回要等它写完，调用方随后删得到这一行。"""
    session_id, call_id = 4242, "call-in-flight"
    command_results.register(
        call_id,
        session_id=session_id,
        run_id=1,
        label="t",
        job=FakeCommandJob(),
    )
    writing, release = threading.Event(), threading.Event()

    def slow_delivery(*_args, **_kwargs) -> bool:
        writing.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(CommandResultRegistry, "_enqueue_result", staticmethod(slow_delivery))
    delivering = threading.Thread(
        target=command_results.complete,
        args=(session_id, call_id, CommandConclusion({"ok": True})),
    )
    delivering.start()
    assert writing.wait(5)
    withdrawn = threading.Event()
    withdrawer = threading.Thread(
        target=lambda: (command_results.withdraw_calls(session_id, {call_id}), withdrawn.set())
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
    assert store.run_last_turn_withdrawn(history.session_id, history.run_id) is False

    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)

    assert store.run_last_turn_withdrawn(history.session_id, history.run_id) is True


def test_fork_cannot_cut_at_a_withdrawn_turn(history, side_effects) -> None:
    """撤回留下了更早调用在这一回合才并入的结果（仍带回合号 2），fork 也不能切到这一回合。"""
    from app.service.session.fork_source import ForkSource

    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)
    rows = SessionContextCrud().get(history.session_id, include_in_context=False)
    source = ForkSource(
        session=depends.get_session_crud().get(history.session_id), rows=rows, runs=[]
    )

    assert any(r.turn == 2 and r.retained for r in rows)
    with pytest.raises(KeyError):
        source.turn_start(2)
    assert source.turn_start(1).sequence == 1


def test_cold_rebuild_is_not_resumable_once_the_last_turn_is_withdrawn(
    history, side_effects
) -> None:
    """可恢复是后端事实：cancelled 的 Run 撤回最后一个回合后，冷重建给出的 ``resumable`` 为假。"""
    from app.assistant_transport.projection.snapshot_rebuild import (
        rebuild_snapshot,
    )

    runs = RunCrud()
    run = runs.get(history.run_id)
    session = SessionCrud().get(history.session_id)
    session.current_run_id = run.id
    cancelled = replace(run, status="cancelled")

    def resumable() -> bool:
        rows = SessionContextCrud().get(history.session_id, include_in_context=False)
        state = rebuild_snapshot(session, [cancelled], rows, {})
        return state["activity"]["resumable"]

    assert resumable() is True
    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)
    assert resumable() is False


def test_plan_withdraws_the_interrupted_draft_with_its_turn() -> None:
    """流式中途停止后撤回：被打断的草稿从不进上下文却照常显示，必须随回合一起移出，否则撤回后用户的话
    没了、半截回复还挂在线程里，之后的分叉还会把它复制过去。"""
    from app.models.message_context.session_context import (
        SessionContextRecord,
        run_last_turn_withdrawn,
    )

    def row(
        row_id: int, turn: int, message: dict, *, include: bool, metadata=None
    ) -> SessionContextRecord:
        return SessionContextRecord(
            session_id=1,
            run_id=7,
            message=message,
            include_in_context=include,
            sequence=row_id,
            turn=turn,
            metadata=metadata or {},
            id=row_id,
        )

    rows = [
        row(
            1,
            1,
            {"role": "user", "content": "写一篇长文"},
            include=True,
            metadata=_user("写一篇长文"),
        ),
        row(2, 1, {"role": "assistant", "content": "## 半截"}, include=False),
    ]

    plan = WithdrawalPlan.plan_withdrawal(rows, from_turn=1)

    assert plan.excluded_row_ids == frozenset({1, 2})
    after = [replace(r, include_in_context=False, project_to_transport=False) for r in rows]
    assert run_last_turn_withdrawn(after)


def test_notices_about_withdrawn_calls_are_dropped_not_redelivered(history, side_effects) -> None:
    """原地结算的占位，结局只在一条带所指调用的通知里：调用被撤回，通知随之作废（队列里删除，合成行里的
    不再重新投递）；关于更早调用的通知照常保留。"""
    queue = depends.get_session_message_queue()
    for call_id in ("call-decide", "call-early"):
        queue.put(
            history.session_id,
            SessionMessage.of(
                NoticeBody(
                    content=f"Your question (tool_call_id={call_id}) was not answered",
                    notice_kind="decision_expired",
                    tool_call_ids=[call_id],
                )
            ),
        )

    depends.get_turn_withdrawal_service().withdraw(history.session_id, 12)

    expired = [
        message.body.tool_call_ids
        for message in queue.pending_nowait(history.session_id)
        if isinstance(message.body, NoticeBody) and message.body.notice_kind == "decision_expired"
    ]
    assert expired == [["call-early"]]


def test_plan_does_not_redeliver_session_notices_about_withdrawn_calls() -> None:
    from app.models.message_context.message_metadata.user_message_metadata import NoticeEntry
    from app.models.message_context.session_context import SessionContextRecord

    notice = UserMessageMetadata(
        kind="notice",
        notices=[
            NoticeEntry(
                notice_kind="decision_expired", content="late", tool_call_ids=["call-late"]
            ),
            NoticeEntry(
                notice_kind="decision_expired", content="early", tool_call_ids=["call-early"]
            ),
            NoticeEntry(notice_kind="backend_restarted", content="restarted"),
        ],
    )
    rows = [
        SessionContextRecord(
            session_id=1,
            run_id=7,
            message=_call("call-early"),
            include_in_context=True,
            sequence=1,
            turn=1,
            id=1,
        ),
        SessionContextRecord(
            session_id=1,
            run_id=7,
            message={"role": "user", "content": "second"},
            include_in_context=True,
            sequence=2,
            turn=2,
            metadata=_user("second"),
            id=2,
        ),
        SessionContextRecord(
            session_id=1,
            run_id=7,
            message=_call("call-late"),
            include_in_context=True,
            sequence=3,
            turn=2,
            id=3,
        ),
        SessionContextRecord(
            session_id=1,
            run_id=7,
            message={"role": "user", "content": "notice"},
            include_in_context=True,
            sequence=4,
            turn=2,
            metadata=notice,
            id=4,
        ),
    ]

    plan = WithdrawalPlan.plan_withdrawal(rows, from_turn=2)

    assert [n.content for n in plan.redeliver] == ["early", "restarted"]
