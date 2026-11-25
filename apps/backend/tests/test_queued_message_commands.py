"""还没并入的用户消息的「立即发送」与「删除」命令：在真实 SQLite 外部队列上验证撤回与重新入队。

立即发送让排队消息原地改为 ``immediate``（身份不变），插话消息原样保留，两者都经队列的待重判集合交给
actor 判定；两者都停下当前执行中的工具调用。删除只撤回。
两者都只作用于仍在队列中的用户输入，已并入或不是用户输入时返回不可重试的 ``MESSAGE_CONSUMED``，
且不改动队列。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.assistant_transport.service import assistant_transport_service as service_module
from app.assistant_transport.service.assistant_transport_service import (
    AssistantTransportService,
)
from app.core import session_actor as actor_module
from app.core.session_actor import SessionActor
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.core.workflow.message_queue.session_message_queue import SessionMessageQueue
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_context.message_metadata.user_message_metadata import UserInputPresentation
from app.models.session.session_command_errors import SessionCommandConflictError

_LAUNCH = RunLaunch(model_config_id="test/m5")


def _input(text: str, delivery: str = "next_run") -> SessionMessage:
    body = UserInputBody(text=text, launch=_LAUNCH, presentation=UserInputPresentation(text=text))
    return SessionMessage.of(body, delivery=delivery)


@pytest.fixture
def harness(canonical_store, monkeypatch):
    queue = SessionMessageQueue()
    # 撤回走真实的 ``SessionActor.withdraw_input``（删行、发通知、拿副本）；actor 其余部分不需要。
    withdrawing = SessionActor.__new__(SessionActor)
    withdrawing.session_id = canonical_store.session.id
    withdrawing.mailbox = RunMailbox()
    monkeypatch.setattr(actor_module, "main_db_factory", lambda: canonical_store.factory)
    monkeypatch.setattr(actor_module.service_depends, "get_session_message_queue", lambda: queue)
    actor = SimpleNamespace(
        accept_immediate_input=AsyncMock(),
        withdraw_input=withdrawing.withdraw_input,
        mailbox=withdrawing.mailbox,
    )
    space = SimpleNamespace(async_operation=nullcontext, actor=actor)
    monkeypatch.setattr(service_module, "main_db_factory", lambda: canonical_store.factory)
    monkeypatch.setattr(
        service_module,
        "session_runtime_spaces",
        SimpleNamespace(
            get_or_create=lambda _id: space,
            enqueue=lambda session_id, message, db=None: queue.put(session_id, message, db=db),
        ),
    )
    # 模型配置替身：列在 ``missing`` 里的模型视为已从配置删除，立即发送同发送一样被拒。
    missing: set[str] = set()

    def check_launch_config(model_config_id: str, _effort: object) -> None:
        if model_config_id in missing:
            raise UserInputRejectedError("MODEL_CONFIG_UNAVAILABLE", "当前模型配置不存在，请重新选择模型")

    monkeypatch.setattr(
        service_module.service_depends,
        "get_model_config_service",
        lambda: SimpleNamespace(check_launch_config=check_launch_config),
    )
    refreshed: list[int] = []
    service = AssistantTransportService.__new__(AssistantTransportService)
    service._queue = queue
    service._sessions = SimpleNamespace(get_session=lambda _session_id: None)
    service._states = SimpleNamespace(refresh_queued_messages=refreshed.append)
    return SimpleNamespace(
        queue=queue,
        service=service,
        refreshed=refreshed,
        actor=actor,
        missing_models=missing,
        session_id=canonical_store.session.id,
    )


async def test_send_now_promotes_in_place_keeping_the_identity(harness) -> None:
    """立即发送原地改为 immediate：身份不变（前端的排队条目、插话气泡与并入后的消息是同一个 id），
    actor 经队列的待重判集合照常判定，不靠删行重插。"""
    first = harness.queue.put(harness.session_id, _input("first"))
    target = harness.queue.put(harness.session_id, _input("target"))

    await harness.service.send_now(harness.session_id, target)

    rows = harness.queue.pending_nowait(harness.session_id)
    assert [(row.id, row.body.text, row.delivery) for row in rows] == [
        (first, "first", "next_run"),
        (target, "target", "immediate"),
    ]
    assert rows[1].body.launch == _LAUNCH
    harness.actor.accept_immediate_input.assert_awaited_once()
    # 原地改过的行交给等待方重判：水位已越过它，也照样算作没判定过。
    read = await harness.queue.wait_unhandled(harness.session_id, after_id=target)
    assert [row.id for row in read.fresh] == [target]


@pytest.mark.parametrize("delivery", ["next_run", "immediate"])
async def test_send_now_rejects_an_input_whose_model_was_deleted(harness, delivery) -> None:
    """模型已删除的输入开不了 Run（调度跳过它）：立即发送同发送一样以 400 拒绝，行原样留着、不交给重判，
    也不打断任何调用——用户据此编辑或删除它，而不是点了没反应。"""
    target = harness.queue.put(harness.session_id, _input("stale", delivery))
    harness.missing_models.add(_LAUNCH.model_config_id)

    with pytest.raises(UserInputRejectedError) as rejected:
        await harness.service.send_now(harness.session_id, target)

    assert rejected.value.code == "MODEL_CONFIG_UNAVAILABLE"
    [row] = harness.queue.pending_nowait(harness.session_id)
    assert (row.id, row.delivery) == (target, delivery)
    harness.actor.accept_immediate_input.assert_not_awaited()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await harness.queue.wait_unhandled(harness.session_id, after_id=target)


async def test_send_now_keeps_an_interjection_and_interrupts_the_running_call(harness, monkeypatch) -> None:
    target = harness.queue.put(harness.session_id, _input("look here", "immediate"))
    cancelled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        service_module.command_results,
        "request_cancel",
        lambda session_id, call_id: cancelled.append((session_id, call_id)) or True,
    )
    tool_call_cancellation_registry.begin(harness.session_id, "call-running")
    try:
        await harness.service.send_now(harness.session_id, target)
    finally:
        tool_call_cancellation_registry.clear(harness.session_id, "call-running")

    [row] = harness.queue.pending_nowait(harness.session_id)
    assert (row.id, row.delivery) == (target, "immediate")
    assert cancelled == [(harness.session_id, "call-running")]
    harness.actor.accept_immediate_input.assert_awaited_once()


async def test_send_now_without_a_running_call_interrupts_nothing(harness, monkeypatch) -> None:
    target = harness.queue.put(harness.session_id, _input("later"))
    monkeypatch.setattr(
        service_module.command_results,
        "request_cancel",
        lambda *_a: pytest.fail("no running call to interrupt"),
    )

    await harness.service.send_now(harness.session_id, target)

    [row] = harness.queue.pending_nowait(harness.session_id)
    assert row.delivery == "immediate"


async def test_delete_withdraws_only_the_target(harness) -> None:
    keep = harness.queue.put(harness.session_id, _input("keep"))
    target = harness.queue.put(harness.session_id, _input("drop"))

    await harness.service.delete_queued(harness.session_id, target)

    assert [row.id for row in harness.queue.pending_nowait(harness.session_id)] == [keep]
    assert harness.refreshed == [harness.session_id]


# 目的：撤回同时拿掉驱动里的副本，并只为打断过工作的输入告诉模型；潜在缺陷：副本残留让同批调用照样让位，
# 或让位已发生却没人告诉模型那条消息不会来了。
@pytest.mark.parametrize("interrupted", [False, True])
async def test_delete_drops_the_delivered_copy_and_tells_only_about_interruptions(harness, interrupted) -> None:
    target = harness.queue.put(harness.session_id, _input("插话", delivery="immediate"))
    [copy] = harness.queue.pending_nowait(harness.session_id)
    harness.actor.mailbox.post(copy)
    if interrupted:
        assert harness.queue.mark_interrupted_work(harness.session_id, [target])

    await harness.service.delete_queued(harness.session_id, target)

    assert harness.actor.mailbox.user_input_ids() == []
    left = harness.queue.pending_nowait(harness.session_id)
    if interrupted:
        [notice] = left
        assert notice.kind == "notice" and notice.body.notice_kind == "input_withdrawn"
        assert "插话" not in notice.body.content
    else:
        assert left == []


# 目的：让位与撤回谁先落定由同一行裁决；潜在缺陷：闸门外读到的副本已被撤回，仍让同批调用让位。
async def test_marking_a_withdrawn_input_does_not_hold(harness) -> None:
    target = harness.queue.put(harness.session_id, _input("插话", delivery="immediate"))
    await harness.service.delete_queued(harness.session_id, target)

    assert harness.queue.mark_interrupted_work(harness.session_id, [target]) is False
    assert harness.queue.pending_nowait(harness.session_id) == []


@pytest.mark.parametrize("command", ["send_now", "delete_queued"])
async def test_consumed_message_is_a_non_retryable_conflict(harness, command) -> None:
    with pytest.raises(SessionCommandConflictError) as caught:
        await getattr(harness.service, command)(harness.session_id, 999)

    assert caught.value.code == "MESSAGE_CONSUMED"
    assert caught.value.retryable is False
    assert harness.refreshed == []
    harness.actor.accept_immediate_input.assert_not_awaited()


@pytest.mark.parametrize("command", ["send_now", "delete_queued"])
async def test_non_user_input_is_rejected_and_kept(harness, command) -> None:
    """撤回在事务里先删后判：不是用户输入时回滚，行保留。"""
    result = harness.queue.put(
        harness.session_id,
        SessionMessage.of(
            BackgroundResultBody(
                ended_at="2026-01-01T00:01:00+00:00",
                tool_call_id="call-probe",
                tool_name="execute_terminal",
                tool_result="x",
            )
        ),
    )

    with pytest.raises(SessionCommandConflictError):
        await getattr(harness.service, command)(harness.session_id, result)

    assert [row.id for row in harness.queue.pending_nowait(harness.session_id)] == [result]
    harness.actor.accept_immediate_input.assert_not_awaited()


async def test_promote_marks_a_pending_user_input_after_commit(harness) -> None:
    """由一条条件更新裁决：只作用于仍在队列里的用户输入；回滚不登记待重判，提交后才交给等待方。已是
    ``immediate`` 的插话也登记：水位可能早已越过它（停止、失败后留下的），不登记就不会再被判定。"""
    queue = harness.queue
    queued = queue.put(harness.session_id, _input("queued"))
    interjection = queue.put(harness.session_id, _input("interjection", "immediate"))
    factory = service_module.main_db_factory()

    with factory.begin() as db:
        assert queue.promote(db, harness.session_id, 999) is None

    db = factory()
    db.begin()
    assert queue.promote(db, harness.session_id, queued) is not None
    db.rollback()
    db.close()
    assert [row.delivery for row in queue.pending_nowait(harness.session_id)] == ["next_run", "immediate"]

    with factory.begin() as db:
        assert queue.promote(db, harness.session_id, queued).delivery == "immediate"
    read = await queue.wait_unhandled(harness.session_id, after_id=interjection)
    assert [row.id for row in read.fresh] == [queued]
    assert read.watermark == interjection

    with factory.begin() as db:
        assert queue.promote(db, harness.session_id, interjection).delivery == "immediate"
    read = await queue.wait_unhandled(harness.session_id, after_id=interjection)
    assert [row.id for row in read.fresh] == [interjection]


@pytest.mark.parametrize("ending", ["user_stop", "failed"])
async def test_send_now_after_the_run_ended_starts_a_run_with_the_stranded_interjection(
    canonical_store, monkeypatch, ending
) -> None:
    """插话投递后、模型读到之前 Run 被停止或失败：不自动接续是设计，但用户随后点「立即发送」要开新 Run。

    真实 SQLite 队列 + 真实 ``SessionActor``；台账与驱动是替身。驱动开始时并入 mailbox 里已有的消息，之后
    投进来的插话留在 mailbox，正如 prepare_input 只在模型请求前并入、取消或失败时不再并入。
    """
    from app.core import session_actor as actor_module
    from app.core import session_execution as execution_module
    from app.core.events.event_sink import set_event_sink
    from app.core.session_actor import SessionActor
    from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
    from app.core.workflow.execution_state.run_mailbox import RunMailbox
    from tests.test_transport_command_chain import _Ledger

    session_id = canonical_store.session.id
    queue, ledger, mailbox = SessionMessageQueue(), _Ledger(), RunMailbox()
    consumed: dict[int, list[int]] = {}
    release = asyncio.Event()

    class Driver:
        def __init__(self, *_args: object) -> None:
            pass

        async def drive(self, run, _preceding_run) -> None:
            ids = []
            while not mailbox.isEmpty():
                ids.append(mailbox.pop().id)
            with canonical_store.factory.begin() as db:
                queue.ack(db, ids)
            consumed[run.id] = ids
            if run.id == 1:
                await release.wait()
            stopped = cancellation_registry.is_cancelled(session_id)
            run.status = "cancelled" if stopped else "failed" if run.id == 1 else "completed"

    monkeypatch.setattr(execution_module, "RunDriver", Driver)
    for getter, fake in (
        (
            "get_model_config_service",
            SimpleNamespace(
                available_ids=lambda: frozenset({_LAUNCH.model_config_id, "test/m1"}),
                check_launch_config=lambda *_args: None,
            ),
        ),
        ("get_run_ledger", ledger),
        ("get_session_service", ledger),
        ("get_session_message_queue", queue),
    ):
        monkeypatch.setattr(actor_module.service_depends, getter, lambda fake=fake: fake)
    monkeypatch.setattr(actor_module, "expire_decisions_after_run", lambda *_args: [])
    monkeypatch.setattr(actor_module.derived_report, "report_settled", lambda _run: None)
    set_event_sink(lambda _event: None)  # 下一个用例由 conftest 的 autouse 夹具换回
    lock = asyncio.Lock()
    space = SimpleNamespace(session_id=session_id, mailbox=mailbox, async_operation=lambda: lock)
    actor = SessionActor(space)
    space.actor = actor
    monkeypatch.setattr(service_module, "main_db_factory", lambda: canonical_store.factory)
    monkeypatch.setattr(
        service_module, "session_runtime_spaces", SimpleNamespace(get_or_create=lambda _id: space)
    )
    service = AssistantTransportService.__new__(AssistantTransportService)
    service._queue, service._sessions = queue, ledger

    async def until(predicate) -> None:
        async with asyncio.timeout(2):
            while not predicate():  # noqa: ASYNC110 - 驱动与水位没有可等的事件
                await asyncio.sleep(0.005)

    actor.start()
    try:
        first = queue.put(session_id, _input("first", "immediate"))
        await until(lambda: consumed.get(1) == [first])
        interjection = queue.put(session_id, _input("interjection", "immediate"))
        await until(lambda: actor._watermark >= interjection)
        if ending == "user_stop":
            await actor.cancel()
        release.set()
        await until(lambda: not actor.driving and ledger.runs[1].status != "running")
        assert [row.id for row in queue.pending_nowait(session_id)] == [interjection]

        await service.send_now(session_id, interjection)
        await until(lambda: consumed.get(2) == [interjection])
        assert queue.pending_nowait(session_id) == []
    finally:
        await actor.abort()
        cancellation_registry.consume(session_id)
