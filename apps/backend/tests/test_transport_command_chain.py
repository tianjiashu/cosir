"""TransportAssistantService 发送、取消、恢复链路的回归测试。

真实的 ``TaskActor``、``cancellation_registry`` 与 ``TransportAssistantService``；外部队列、
Run 台账与驱动是替身。驱动替身按生产语义收敛取消：每次进入模型边界前先看 Task 取消信号，有信号就
落定 cancelled、不取 mailbox（消息留在外部队列）。Task 取消信号由 actor 在执行收尾时取走（唯一
清理点），驱动替身不碰它。

覆盖曾经的六个缺陷：取消挂起 Run 不生效、停止中发送的消息被卡住、无执行者时登记的信号残留、
级联取消原因、用户取消通知、取消在事件循环线程上同步读库。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.service import transport_assistant_service as service_module
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.core import task_actor as actor_module
from app.core import task_execution as execution_module
from app.core.task_actor import TaskActor
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.task_message import (
    BackgroundResultBody,
    NoticeBody,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.models.conversation_input import UserInputPresentation
from app.models.enums.conversation_run_status import RunTransition
from app.core.tool.handler.agent_team import coordinator as coordinator_module
from app.task_runtime import task_runtime_space_registry as registry_module

_TASK_ID = 41
_CHILD_TASK_ID = 42


class _Queue:
    """按水位阻塞的外部队列替身；行 id 递增，``ack`` 模拟 prepare_input 的确认删除。"""

    def __init__(self) -> None:
        self.rows: list[TaskMessage] = []
        self._next_id = 1
        self._changed = asyncio.Event()

    def put(self, _task_id: int, message: TaskMessage, **_kw: Any) -> int:
        stored = TaskMessage.of(
            message.body,
            delivery=message.delivery,
            project_to_transport=message.project_to_transport,
            message_id=self._next_id,
        )
        self._next_id += 1
        self.rows.append(stored)
        self._changed.set()
        return stored.id

    def ack(self, *ids: int) -> None:
        self.rows = [row for row in self.rows if row.id not in ids]

    async def pending(self, _task_id: int, after_id: int = 0) -> list[TaskMessage]:
        while not any(row.id > after_id for row in self.rows):
            self._changed.clear()
            await self._changed.wait()
        return list(self.rows)

    def pending_nowait(self, _task_id: int) -> list[TaskMessage]:
        return list(self.rows)


class _Ledger:
    """Run 台账与 Task 当前 Run 替身；迁移按来源状态条件生效。"""

    def __init__(self) -> None:
        self.runs: dict[int, SimpleNamespace] = {}
        self.current: SimpleNamespace | None = None

    def create_run(
        self, task_id: int, launch: RunLaunch
    ) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            task_id=task_id,
            status="running",
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            options=launch.options,
        )
        self.runs[run.id] = run
        self.current = run
        return run

    def transition(self, run_id: int, transition: RunTransition, **_kw: Any):
        run = self.runs[run_id]
        sources, target, _ = transition.value
        if run.status not in {source.value for source in sources}:
            return None
        run.status = target.value
        return run

    def get_run(self, run_id: int) -> SimpleNamespace:
        return self.runs[run_id]

    def require_current_run(self, _task_id: int) -> SimpleNamespace | None:
        return self.current

    def list_child_tasks(self, _task_id: int) -> list:
        return []

    def get_task(self, task_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=task_id)


class _Driver:
    """驱动替身：进入模型边界前判定取消；否则取光 mailbox 并确认，等测试给出落定状态。"""

    queue: _Queue
    ledger: _Ledger
    finish: dict[int, asyncio.Event]
    settle: dict[int, str]
    # Run 落定后执行者继续收尾（STOP Hook 等）直到事件置位；未登记的 Run 落定即返回。
    linger: dict[int, asyncio.Event]
    consumed: list[int]
    # 本次执行的 mailbox 归 space 持有（``RunDriver`` 不再接收它），由 fixture 注入。
    mailbox: RunMailbox = RunMailbox()

    def __init__(self, *_args: object) -> None:
        self._mailbox = _Driver.mailbox

    async def drive(self, run) -> None:
        if cancellation_registry.is_cancelled(run.task_id):
            # prepare_input：取消优先，不取 mailbox，消息留在外部队列。
            _Driver.ledger.transition(run.id, RunTransition.CANCEL)
            return
        drained = []
        while not self._mailbox.isEmpty():
            drained.append(self._mailbox.pop().id)
        ids = [message_id for message_id in drained if message_id is not None]
        _Driver.queue.ack(*ids)
        _Driver.consumed.extend(ids)
        event = _Driver.finish.setdefault(run.id, asyncio.Event())
        await event.wait()
        _Driver.ledger.runs[run.id].status = _Driver.settle.get(run.id, "completed")
        if run.id in _Driver.linger:
            await _Driver.linger[run.id].wait()


def _user(text: str = "hi") -> UserInputBody:
    return UserInputBody(
        input_text=text,
        launch=RunLaunch(model_config_id=1),
        presentation=UserInputPresentation(display_text=text),
    )


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


async def _finish(run_id: int, status: str) -> None:
    _Driver.settle[run_id] = status
    _Driver.finish.setdefault(run_id, asyncio.Event()).set()
    await _settle()


@pytest.fixture
async def chain(monkeypatch):
    queue, ledger = _Queue(), _Ledger()
    _Driver.queue, _Driver.ledger = queue, ledger
    _Driver.finish, _Driver.settle, _Driver.consumed, _Driver.linger = {}, {}, [], {}
    monkeypatch.setattr(execution_module, "RunDriver", _Driver)
    monkeypatch.setattr(actor_module, "_republish", lambda _task_id: None)
    # 推进前刷新排队投影；本文件不验证投影（见 test_task_actor_loop 的「先刷新再投递」）。
    states = SimpleNamespace(refresh_queued_messages=lambda _task_id: None)
    for getter, fake in (
        ("get_conversation_run_ledger", ledger),
        ("get_task_service", ledger),
        ("get_task_message_queue", queue),
        ("get_conversation_task_state_service", states),
    ):
        monkeypatch.setattr(actor_module.service_depends, getter, lambda fake=fake: fake)
    actor = TaskActor(
        SimpleNamespace(
            task_id=_TASK_ID,
            async_operation=contextlib.nullcontext,
            mailbox=RunMailbox(),
        )
    )
    _Driver.mailbox = actor.mailbox
    space = SimpleNamespace(actor=actor)
    child_cancels: list[tuple[int, str]] = []
    monkeypatch.setattr(
        service_module,
        "task_runtime_spaces",
        SimpleNamespace(
            get_or_create=lambda _task_id: space,
            enqueue=lambda task_id, message, **_kw: queue.put(task_id, message),
        ),
    )
    # actor 收尾时经注册表停止子 Task、经 Coordinator 停止 Agent Team。
    monkeypatch.setattr(
        registry_module.task_runtime_spaces,
        "cancel",
        lambda task_id, reason: child_cancels.append((task_id, reason)),
    )
    cancelled_teams: list[int] = []
    monkeypatch.setattr(
        coordinator_module,
        "get_agent_team_coordinator",
        lambda: SimpleNamespace(cancel_for_parent_task=cancelled_teams.append),
    )
    service = TransportAssistantService.__new__(TransportAssistantService)
    service._tasks = ledger
    service._inputs = SimpleNamespace(
        prepare=lambda task_id, command, model_config_id, reasoning_effort=None,
        agent_id="main_agent", *, delivery="immediate", reuse=None: TaskMessage.of(
            UserInputBody(
                input_text=command.display_text,
                launch=RunLaunch(agent_id=agent_id, model_config_id=model_config_id),
                presentation=UserInputPresentation(display_text=command.display_text),
            ),
            delivery=delivery,
        )
    )
    actor.start()
    yield SimpleNamespace(
        actor=actor,
        queue=queue,
        ledger=ledger,
        service=service,
        child_cancels=child_cancels,
        cancelled_teams=cancelled_teams,
    )
    await actor.abort()
    tool_call_cancellation_registry.clear_task(_TASK_ID)
    cancellation_registry.consume(_TASK_ID)
    cancellation_registry.consume(_CHILD_TASK_ID)


def _send_request(text: str, delivery: str = "immediate"):
    from app.assistant_transport.request import SendMessageRequest

    return SendMessageRequest.model_validate(
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
            "modelConfigId": 1,
            "delivery": delivery,
        }
    )


# ---------------------------------------------------------------------------
# 执行阶段：活动判断看执行者，不只看 Run 状态
# ---------------------------------------------------------------------------


async def test_phase_reads_the_run_on_a_freshly_started_actor(chain) -> None:
    """actor 刚拉起、从未推进过：阶段按当前 Run 判定，不依赖推进时缓存的标志。"""
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "completed"
    assert await chain.actor.phase() == "idle"
    run.status = "suspended"
    assert await chain.actor.phase() == "suspended"


async def test_phase_stays_driving_until_the_executor_is_released(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    run = chain.ledger.current
    _Driver.linger[run.id] = asyncio.Event()

    await _finish(run.id, "completed")
    assert run.status == "completed" and await chain.actor.phase() == "driving"

    _Driver.linger[run.id].set()
    await _settle()
    assert await chain.actor.phase() == "idle"


async def test_tool_call_cancel_needs_an_executor_not_an_active_status(chain) -> None:
    """挂起的 Run 没有执行者：工具调用都已闭合，标记了也没人取，返回 False。"""
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"
    assert chain.service.cancel_tool_call(_TASK_ID, "call-1") is False

    await chain.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    assert chain.actor.driving
    assert chain.service.cancel_tool_call(_TASK_ID, "call-1") is True
    await _finish(chain.ledger.current.id, "completed")


def _register_deferred(tool_call_id: str, mode: str, calls: list[str]) -> None:
    from app.core.tool.background.deferred_registry import deferred_registry

    deferred_registry.register(
        tool_call_id,
        task_id=_TASK_ID,
        run_id=None,
        kind="terminal_command",
        label="terminal_command:t",
        mode=mode,  # type: ignore[arg-type]
        on_cancel=lambda: calls.append("cancel"),
        on_detach=(lambda: calls.append("detach")) if mode == "foreground" else None,
    )


async def test_cancel_deferred_background_settles_placeholder_and_leaves_notice(
    chain, monkeypatch
) -> None:
    """后台工作不要求 Run 在执行：撤销登记、占位推到 cancelled，并留一条不唤醒的通知。"""
    settled: list[tuple[int, list[str], str]] = []
    monkeypatch.setattr(
        service_module.placeholders,
        "settle_placeholders",
        lambda task_id, ids, status, _hint: settled.append((task_id, list(ids), status)) or 1,
    )
    calls: list[str] = []
    _register_deferred("call-bg", "background", calls)

    assert chain.service.cancel_tool_call(_TASK_ID, "call-bg") is True

    assert calls == ["cancel"]
    assert settled == [(_TASK_ID, ["call-bg"], "cancelled")]
    [notice] = chain.queue.pending_nowait(_TASK_ID)
    assert notice.kind == "system_notice" and "call-bg" in notice.body.content
    assert chain.service.cancel_tool_call(_TASK_ID, "call-bg") is False


async def test_cancel_deferred_foreground_only_wakes_the_waiter(chain, monkeypatch) -> None:
    monkeypatch.setattr(
        service_module.placeholders,
        "settle_placeholders",
        lambda *_a: pytest.fail("foreground entries have no placeholder"),
    )
    calls: list[str] = []
    _register_deferred("call-fg", "foreground", calls)

    assert chain.service.cancel_tool_call(_TASK_ID, "call-fg") is True

    assert calls == ["cancel"]
    assert chain.queue.pending_nowait(_TASK_ID) == []


async def test_background_command_detaches_only_foreground_entries(chain) -> None:
    from app.core.task_actor import SessionConflictError

    calls: list[str] = []
    _register_deferred("call-fg", "foreground", calls)
    chain.service.background_tool_call(_TASK_ID, "call-fg")
    assert calls == ["detach"]
    for call_id in ("call-fg", "call-missing"):
        with pytest.raises(SessionConflictError) as raised:
            chain.service.background_tool_call(_TASK_ID, call_id)
        assert raised.value.code == "TOOL_CALL_NOT_DETACHABLE"


# ---------------------------------------------------------------------------
# 基线：链路在常规路径上成立（确认替身可信）
# ---------------------------------------------------------------------------


async def test_baseline_send_starts_a_run_and_cancel_stops_it(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    run = chain.ledger.current
    assert run.status == "running" and _Driver.consumed == [1]

    await chain.service.cancel(_TASK_ID)
    assert cancellation_registry.is_cancelled(_TASK_ID)
    await _finish(run.id, "cancelled")
    assert run.status == "cancelled"
    assert not cancellation_registry.is_cancelled(_TASK_ID)


async def test_baseline_resume_restarts_the_cancelled_run(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "cancelled"

    await chain.service.resume(_TASK_ID)
    await _settle()
    assert run.status == "running"


# ---------------------------------------------------------------------------
# 取消挂起中的 Run：直接落定 cancelled，之后的消息照常处理
# ---------------------------------------------------------------------------


async def test_cancel_settles_a_suspended_run(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"

    await chain.service.cancel(_TASK_ID)
    await _settle()

    assert run.status == "cancelled"


async def test_message_after_cancelling_a_suspended_run_is_processed(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"
    await chain.service.cancel(_TASK_ID)

    await chain.service.send(_TASK_ID, _send_request("继续"))
    await _settle()

    # 取消落定时排了一条「用户已取消」通知（id 1）；用户这条消息（id 2）开出新 Run，两条一并并入。
    assert run.status == "cancelled"
    assert chain.ledger.current.id != run.id
    assert _Driver.consumed == [1, 2]


# ---------------------------------------------------------------------------
# 停止过程中发送的消息在停止后照常处理
# ---------------------------------------------------------------------------


async def test_message_sent_while_stopping_is_processed_after_the_stop(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    first_run = chain.ledger.current

    await chain.service.cancel(_TASK_ID)
    await chain.service.send(_TASK_ID, _send_request("after stop"))
    await _settle()
    await _finish(first_run.id, "cancelled")

    # 停止中 loop 不往即将作废的 mailbox 投递，等执行落定后按 idle 判定：这条消息是新消息，
    # 开出新 Run 处理。
    assert len(chain.ledger.runs) == 2
    assert 2 in _Driver.consumed


# ---------------------------------------------------------------------------
# 没有执行者时不登记取消信号：不会杀死下一个 Run（发送与恢复）
# ---------------------------------------------------------------------------


async def test_cancel_after_the_run_finished_registers_no_signal(chain) -> None:
    """Run 已完成才到的停止（如停止与完成竞态）：没有可停止的对象，不登记信号。"""
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    await _finish(chain.ledger.current.id, "completed")

    await chain.service.cancel(_TASK_ID)
    assert not cancellation_registry.is_cancelled(_TASK_ID)

    await chain.service.send(_TASK_ID, _send_request("next"))
    await _settle()
    assert chain.ledger.current.status == "running"
    assert len(chain.ledger.runs) == 2


async def test_cancel_of_a_failed_run_does_not_kill_its_resume(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "failed"

    await chain.service.cancel(_TASK_ID)
    assert not cancellation_registry.is_cancelled(_TASK_ID)

    await chain.service.resume(_TASK_ID)
    await _settle()
    assert run.status == "running"


async def test_signal_is_cleared_when_the_cancelled_execution_finishes(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    run = chain.ledger.current

    await chain.service.cancel(_TASK_ID)
    assert cancellation_registry.is_cancelled(_TASK_ID)
    await _finish(run.id, "cancelled")

    assert not cancellation_registry.is_cancelled(_TASK_ID)


# ---------------------------------------------------------------------------
# 级联取消：Run 落定 cancelled 后以 parent 原因停止下游
# ---------------------------------------------------------------------------


async def test_cancelled_run_stops_downstream_with_parent_reason(chain) -> None:
    chain.ledger.list_child_tasks = lambda _task_id: [SimpleNamespace(id=_CHILD_TASK_ID)]
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    run = chain.ledger.current

    await chain.service.cancel(_TASK_ID)
    # Team 在发起停止时就取消：agent_team 工具阻塞在 Team 上，等 Run 落定会互等。
    assert chain.cancelled_teams == [_TASK_ID]
    # 子 Task 不阻塞父 Run：等 Run 真正落定 cancelled 后才传播。
    assert chain.child_cancels == []
    await _finish(run.id, "cancelled")

    assert chain.child_cancels == [(_CHILD_TASK_ID, "parent")]
    # 落定后不再重复取消 Team。
    assert chain.cancelled_teams == [_TASK_ID]


async def test_cancelling_a_suspended_run_stops_downstream_immediately(chain) -> None:
    chain.ledger.list_child_tasks = lambda _task_id: [SimpleNamespace(id=_CHILD_TASK_ID)]
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"

    await chain.service.cancel(_TASK_ID)

    assert chain.child_cancels == [(_CHILD_TASK_ID, "parent")]
    # 挂起的 Run 常带着待确认方案：停止即放弃本 Task 的全部 Team 工作。
    assert chain.cancelled_teams == [_TASK_ID]


# ---------------------------------------------------------------------------
# 用户取消给下一个 Run 留通知
# ---------------------------------------------------------------------------


async def test_user_cancel_leaves_a_notice_for_the_next_run(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    run = chain.ledger.current

    await chain.service.cancel(_TASK_ID)
    await _finish(run.id, "cancelled")

    notices = [row for row in chain.queue.rows if isinstance(row.body, NoticeBody)]
    assert [(row.body.notice_kind, row.delivery) for row in notices] == [
        ("user_cancelled", "next_run")
    ]


async def test_parent_stop_leaves_no_user_notice(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    run = chain.ledger.current

    await chain.actor.cancel("parent")
    await _finish(run.id, "cancelled")

    assert not any(isinstance(row.body, NoticeBody) for row in chain.queue.rows)


# ---------------------------------------------------------------------------
# 取消不在事件循环线程上同步读库
# ---------------------------------------------------------------------------


async def test_cancel_reads_state_off_the_event_loop(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = "suspended"
    loop_thread = threading.get_ident()
    threads: list[int] = []
    original = chain.ledger.require_current_run

    def recording(task_id: int):
        threads.append(threading.get_ident())
        return original(task_id)

    chain.ledger.require_current_run = recording
    await chain.service.cancel(_TASK_ID)

    assert threads and all(ident != loop_thread for ident in threads)


# ---------------------------------------------------------------------------
# 对照：后台结果在停止后到达会开新 Run（「有消息就处理」的既定行为，不是缺陷）
# ---------------------------------------------------------------------------


async def test_background_result_after_stop_starts_a_run(chain) -> None:
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    await chain.service.cancel(_TASK_ID)
    await _finish(chain.ledger.current.id, "cancelled")

    chain.queue.put(
        _TASK_ID, TaskMessage.of(BackgroundResultBody(tool_name="probe", tool_result="done"))
    )
    await _settle()

    assert len(chain.ledger.runs) == 2


# ---------------------------------------------------------------------------
# 编辑重跑：先校验再动数据；查 Run 表等停；闸门内软删除目标及其后 context 并入队
# ---------------------------------------------------------------------------


class _Context:
    """context 替身：按 sequence 排列的行，软删除只翻 ``include_in_context``。"""

    def __init__(self, *kinds: str) -> None:
        self.rows = [
            SimpleNamespace(
                sequence=sequence,
                message_id=sequence,
                include_in_context=True,
                metadata=SimpleNamespace(kind=kind, presentation=f"p{sequence}"),
            )
            for sequence, kind in enumerate(kinds, start=1)
        ]

    def latest_user_input(self, _task_id: int, session: Any = None):
        for row in reversed(self.rows):
            if row.include_in_context and row.metadata.kind == "user_input":
                return row
        return None

    def exclude_from(self, _task_id: int, sequence: int, _session: Any) -> None:
        for row in self.rows:
            if row.sequence >= sequence:
                row.include_in_context = False

    def included(self) -> list[int]:
        return [row.sequence for row in self.rows if row.include_in_context]


@pytest.fixture
def rerun(chain, monkeypatch):
    context = _Context("user_input", "assistant", "user_input", "assistant")
    reuses: list[Any] = []
    republished: list[int] = []
    prepare = chain.service._inputs.prepare

    def recording_prepare(*args, reuse=None, **kwargs):
        reuses.append(reuse)
        return prepare(*args, reuse=reuse, **kwargs)

    chain.service._context = context
    chain.service._inputs = SimpleNamespace(prepare=recording_prepare)
    chain.service._states = SimpleNamespace(
        rebuild_state=lambda task_id: task_id,
        publish_state=lambda _id, state: republished.append(state),
    )
    monkeypatch.setattr(
        service_module,
        "main_session_factory",
        lambda: SimpleNamespace(begin=contextlib.nullcontext),
    )
    chain.space = service_module.task_runtime_spaces.get_or_create(_TASK_ID)
    chain.space.async_operation = contextlib.nullcontext
    chain.context, chain.reuses, chain.republished = context, reuses, republished
    return chain


def _rerun_request(text: str):
    from app.assistant_transport.request import RerunMessageRequest

    return RerunMessageRequest.model_validate(
        {"message": {"role": "user", "parts": [{"type": "text", "text": text}]}, "modelConfigId": 1}
    )


def _finished_run(chain, status: str = "completed"):
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id=1))
    run.status = status
    return run


async def test_rerun_soft_deletes_target_and_later_rows_then_starts_a_run(rerun) -> None:
    old = _finished_run(rerun)

    await rerun.service.rerun(_TASK_ID, 3, _rerun_request("改过的"))
    await _settle()

    assert rerun.context.included() == [1, 2]
    assert rerun.reuses == ["p3"]  # 复用原消息的展示绑定
    assert rerun.republished == [_TASK_ID]
    assert rerun.ledger.current.id != old.id and rerun.ledger.current.status == "running"
    assert _Driver.consumed == [1]


async def test_rerun_of_a_non_latest_input_is_rejected_untouched(rerun) -> None:
    _finished_run(rerun)

    with pytest.raises(actor_module.SessionConflictError) as error:
        await rerun.service.rerun(_TASK_ID, 1, _rerun_request("改旧的"))

    assert error.value.code == "EDIT_TARGET_STALE"
    assert rerun.context.included() == [1, 2, 3, 4] and rerun.queue.rows == []


async def test_rerun_with_invalid_input_leaves_run_and_context_untouched(rerun) -> None:
    run = _finished_run(rerun, "suspended")

    def reject(*_args, **_kwargs):
        raise ValueError("bad input")

    rerun.service._inputs = SimpleNamespace(prepare=reject)
    with pytest.raises(ValueError):
        await rerun.service.rerun(_TASK_ID, 3, _rerun_request("x"))

    assert run.status == "suspended"
    assert rerun.context.included() == [1, 2, 3, 4] and rerun.queue.rows == []


async def test_rerun_is_rejected_while_running_without_stopping_the_run(rerun) -> None:
    """重跑不替用户停止：运行中的 Run 用的是内存上下文，看不到移出，只能拒绝。"""
    await rerun.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    old = rerun.ledger.current

    with pytest.raises(actor_module.SessionConflictError) as error:
        await rerun.service.rerun(_TASK_ID, 3, _rerun_request("改过的"))

    assert error.value.code == "RUN_NOT_STOPPED"
    assert not cancellation_registry.is_cancelled(_TASK_ID)
    assert old.status == "running" and rerun.context.included() == [1, 2, 3, 4]
    await _finish(old.id, "completed")


async def test_rerun_is_rejected_while_suspended(rerun) -> None:
    old = _finished_run(rerun, "suspended")

    with pytest.raises(actor_module.SessionConflictError) as error:
        await rerun.service.rerun(_TASK_ID, 3, _rerun_request("改过的"))

    assert error.value.code == "RUN_NOT_STOPPED"
    assert old.status == "suspended" and rerun.context.included() == [1, 2, 3, 4]


async def test_rerun_is_rejected_while_the_executor_is_still_settling(rerun) -> None:
    """Run 已落定 cancelled、执行者还在收尾：此时改 context 会与收尾交错，按执行阶段拒绝。"""
    await rerun.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    old = rerun.ledger.current
    _Driver.linger[old.id] = asyncio.Event()
    await _finish(old.id, "cancelled")
    assert old.status == "cancelled"

    with pytest.raises(actor_module.SessionConflictError) as error:
        await rerun.service.rerun(_TASK_ID, 3, _rerun_request("改过的"))
    assert error.value.code == "RUN_NOT_STOPPED"
    assert rerun.context.included() == [1, 2, 3, 4]

    _Driver.linger[old.id].set()
    await _settle()
    await rerun.service.rerun(_TASK_ID, 3, _rerun_request("改过的"))
    await _settle()
    assert rerun.context.included() == [1, 2]
    assert rerun.ledger.current.id != old.id and rerun.ledger.current.status == "running"
