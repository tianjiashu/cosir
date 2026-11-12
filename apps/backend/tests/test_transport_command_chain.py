"""AssistantTransportService 发送、取消、恢复链路的回归测试。

真实的 ``TaskActor``、``cancellation_registry`` 与 ``AssistantTransportService``；外部队列、
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

from app.assistant_transport.service import assistant_transport_service as service_module
from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core import task_actor as actor_module
from app.core import task_execution as execution_module
from app.core import task_recall as recall_module
from app.core.events.event_sink import set_event_sink
from app.core.task_actor import TaskActor
from app.core.tool import placeholder_settlement as placeholder_module
from app.core.tool.placeholder_settlement import USER_CANCELLED_HINT
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
from app.models.errors.task_command_errors import TaskCommandConflictError
from app.models.task_extra import TaskExtra
from app.task_runtime import task_runtime_space_registry as registry_module
from tests.command_job_fakes import FakeCommandJob

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

    def create_run(self, task_id: int, launch: RunLaunch) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            task_id=task_id,
            status="running",
            agent_id=launch.agent_id,
            end_reason=None,
            model_config_id=launch.model_config_id,
            options=launch.options,
        )
        self.runs[run.id] = run
        self.current = run
        return run

    def transition(
        self, run_id: int, transition: RunTransition, *, end_reason: str | None = None, **_kw: Any
    ):
        run = self.runs[run_id]
        sources, target, _ = transition.value
        if run.status not in {source.value for source in sources}:
            return None
        run.status = target.value
        run.end_reason = end_reason
        return run

    def get_run(self, run_id: int) -> SimpleNamespace:
        return self.runs[run_id]

    def require_current_run(self, _task_id: int) -> SimpleNamespace | None:
        return self.current

    def list_derived_tasks(self, _task_id: int) -> list:
        return []

    def get_task(self, task_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=task_id, extra=TaskExtra())


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

    async def drive(self, run, preceding_run) -> None:
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
        text=text,
        launch=RunLaunch(model_config_id="test/m1"),
        presentation=UserInputPresentation(text=text),
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
    # 恢复前查这个 Run 的最后一个回合是否已撤回；本文件不撤回（撤回语义见 test_turn_withdrawal）。
    chain_withdrawn: set[int] = set()
    monkeypatch.setattr(
        actor_module,
        "ContextStore",
        lambda: SimpleNamespace(
            run_last_turn_withdrawn=lambda _task_id, run_id: run_id in chain_withdrawn
        ),
    )
    # 推进前交出「队列变了」；本文件不验证投影（见 test_task_actor_loop 的「先刷新再投递」）。
    set_event_sink(lambda _event: None)  # 下一个用例由 conftest 的 autouse 夹具换回
    # 开新 Run 前 actor 查模型是否还在；本文件的模型总在（悬空 id 见 test_task_actor_loop）。
    models = SimpleNamespace(exists=lambda _model_config_id: True)
    for getter, fake in (
        ("get_model_config_service", models),
        ("get_conversation_run_ledger", ledger),
        ("get_task_service", ledger),
        ("get_task_message_queue", queue),
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
    space = SimpleNamespace(actor=actor, async_operation=contextlib.nullcontext)
    child_cancels: list[tuple[int, str]] = []
    monkeypatch.setattr(
        service_module,
        "task_runtime_spaces",
        SimpleNamespace(
            get_or_create=lambda _task_id: space,
            enqueue=lambda task_id, message, **_kw: queue.put(task_id, message),
        ),
    )
    # 收回时经子 Task 的 actor 停止子 Task、经 Coordinator 停止 Agent Team。
    monkeypatch.setattr(
        registry_module.task_runtime_spaces,
        "get_or_create",
        lambda task_id: SimpleNamespace(
            actor=SimpleNamespace(
                request_cancel=lambda reason: child_cancels.append((task_id, reason))
            )
        ),
    )
    cancelled_teams: list[int] = []

    async def cancel_teams(task_id):
        cancelled_teams.append(task_id)

    monkeypatch.setattr(
        recall_module.service_depends,
        "get_agent_team_coordinator",
        lambda: SimpleNamespace(cancel_for_parent_task=cancel_teams),
    )
    service = AssistantTransportService.__new__(AssistantTransportService)
    service._tasks = ledger
    service._inputs = SimpleNamespace(
        prepare=lambda _task_id, command, model_config_id, _reasoning_effort, agent_id: UserInputBody(
            text=command.text,
            launch=RunLaunch(agent_id=agent_id, model_config_id=model_config_id),
            presentation=UserInputPresentation(text=command.text),
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
        withdrawn=chain_withdrawn,
    )
    await actor.abort()
    tool_call_cancellation_registry.clear_task(_TASK_ID)
    cancellation_registry.consume(_TASK_ID)
    cancellation_registry.consume(_CHILD_TASK_ID)


def _send_request(text: str):
    from app.assistant_transport.request import SendMessageRequest

    return SendMessageRequest.model_validate(
        {
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
            "modelConfigId": "test/m1",
        }
    )


# ---------------------------------------------------------------------------
# 执行阶段：活动判断看执行者，不只看 Run 状态
# ---------------------------------------------------------------------------


async def test_phase_reads_the_run_on_a_freshly_started_actor(chain) -> None:
    """actor 刚拉起、从未推进过：阶段按当前 Run 判定，不依赖推进时缓存的标志。"""
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
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


async def test_sends_while_running_queue_and_drain_one_per_run(chain) -> None:
    """运行中发送排队（``next_run``）不并入当前 Run；Run 完成后一轮只消费一条，依次接续。"""
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    first_run = chain.ledger.current
    assert len(_Driver.consumed) == 1

    await chain.service.send(_TASK_ID, _send_request("second"))
    await chain.service.send(_TASK_ID, _send_request("third"))
    await _settle()
    assert [row.delivery for row in chain.queue.rows] == ["next_run", "next_run"]
    assert len(_Driver.consumed) == 1

    await _finish(first_run.id, "completed")
    second_run = chain.ledger.current
    assert second_run.id != first_run.id
    assert [row.body.text for row in chain.queue.rows] == ["third"]

    await _finish(second_run.id, "completed")
    assert chain.ledger.current.id != second_run.id
    assert chain.queue.rows == []
    await _finish(chain.ledger.current.id, "completed")


async def test_queued_inputs_wait_after_cancel_and_a_new_send_runs_first(chain) -> None:
    """取消不自动接续；空闲时新发送的是 ``immediate``，先于留下的排队消息被新 Run 选中。"""
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    await chain.service.send(_TASK_ID, _send_request("queued"))
    # 等 loop 读到排队消息再停止：停止之后才读到的消息算停止后的新消息，会开新 Run。
    await _settle()
    await chain.service.cancel(_TASK_ID)
    await _finish(chain.ledger.current.id, "cancelled")
    assert chain.ledger.current.status == "cancelled"
    # 队列里还有用户取消留下的通知（随下一个 Run 并入）。
    assert [row.body.text for row in chain.queue.rows if isinstance(row.body, UserInputBody)] == [
        "queued"
    ]

    await chain.service.send(_TASK_ID, _send_request("fresh"))
    await _settle()
    assert chain.ledger.current.status == "running"
    assert [row.body.text for row in chain.queue.rows if isinstance(row.body, UserInputBody)] == [
        "queued"
    ]
    await _finish(chain.ledger.current.id, "completed")


async def test_tool_call_cancel_needs_an_executor_not_an_active_status(chain) -> None:
    """挂起的 Run 没有执行者：工具调用都已闭合，标记了也没人取，409 TOOL_CALL_NOT_CANCELLABLE。"""
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"
    with pytest.raises(TaskCommandConflictError) as rejected:
        chain.service.cancel_tool_call(_TASK_ID, "call-1")
    assert rejected.value.code == "TOOL_CALL_NOT_CANCELLABLE"

    # 挂起时用户发送只排队、不唤醒：仍没有执行者。
    await chain.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    assert not chain.actor.driving
    assert [row.delivery for row in chain.queue.rows] == ["next_run"]

    chain.queue.put(
        _TASK_ID,
        TaskMessage.of(
            BackgroundResultBody(
                ended_at="2026-01-01T00:01:00+00:00", tool_call_id="call-probe",
                tool_name="execute_terminal", tool_result="done",
            )
        ),
    )
    await _settle()
    assert chain.actor.driving
    chain.service.cancel_tool_call(_TASK_ID, "call-1")
    await _finish(chain.ledger.current.id, "completed")


def _register_background_work(tool_call_id: str, mode: str, calls: list[str]) -> threading.Event:
    from app.tools.tool_handler.terminal.command_results import command_results

    detached = threading.Event()
    command_results.register(
        tool_call_id,
        task_id=_TASK_ID,
        run_id=None,
        label="terminal_command:t",
        job=FakeCommandJob(lambda: calls.append("cancel")),
        detached=detached if mode == "foreground" else None,
    )
    return detached


async def test_cancel_background_work_while_idle_settles_now_and_tells_the_next_drive(
    chain, monkeypatch
) -> None:
    """后台工作不要求 Run 在执行：撤销登记、占位立即推到 cancelled；回执留在队列里随下一次推进并入，
    但不凭它开新 Run。"""
    settled: list[tuple[int, list[str], str | None]] = []
    monkeypatch.setattr(
        placeholder_module,
        "cancel_placeholders",
        lambda task_id, ids, hint, **kwargs: settled.append((task_id, list(ids), hint)) or 1,
    )
    calls: list[str] = []
    _register_background_work("call-bg", "background", calls)

    chain.service.cancel_tool_call(_TASK_ID, "call-bg")

    assert calls == ["cancel"]
    assert settled == [(_TASK_ID, ["call-bg"], USER_CANCELLED_HINT)]
    [receipt] = chain.queue.pending_nowait(_TASK_ID)
    assert receipt.kind == "background_result" and not receipt.wakes(None)
    with pytest.raises(TaskCommandConflictError):
        chain.service.cancel_tool_call(_TASK_ID, "call-bg")


async def test_cancel_background_work_foreground_only_wakes_the_waiter(chain, monkeypatch) -> None:
    monkeypatch.setattr(
        placeholder_module,
        "cancel_placeholders",
        lambda *_a: pytest.fail("foreground entries have no placeholder"),
    )
    calls: list[str] = []
    _register_background_work("call-fg", "foreground", calls)

    chain.service.cancel_tool_call(_TASK_ID, "call-fg")

    assert calls == ["cancel"]
    assert chain.queue.pending_nowait(_TASK_ID) == []


async def test_background_command_detaches_only_foreground_entries(chain) -> None:
    calls: list[str] = []
    detached = _register_background_work("call-fg", "foreground", calls)
    chain.service.background_tool_call(_TASK_ID, "call-fg")
    assert detached.is_set()
    for call_id in ("call-fg", "call-missing"):
        with pytest.raises(TaskCommandConflictError) as raised:
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
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "cancelled"

    await chain.service.resume(_TASK_ID)
    await _settle()
    assert run.status == "running"


# ---------------------------------------------------------------------------
# 取消挂起中的 Run：直接落定 cancelled，之后的消息照常处理
# ---------------------------------------------------------------------------


async def test_cancel_settles_a_suspended_run(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"

    await chain.service.cancel(_TASK_ID)
    await _settle()

    assert run.status == "cancelled"


async def test_message_after_cancelling_a_suspended_run_is_processed(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"
    await chain.service.cancel(_TASK_ID)

    await chain.service.send(_TASK_ID, _send_request("继续"))
    await _settle()

    # 用户这条消息开出新 Run；「用户已停止」不入队，由新 Run 的驱动按上一个 Run 的结束原因推导。
    assert run.status == "cancelled"
    assert chain.ledger.current.id != run.id
    assert _Driver.consumed == [1]


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
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
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
# 用户停止只打断；上游停止（parent）另外收回 Team、后台工作与子 Task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["running", "suspended", "idle"])
async def test_user_stop_leaves_teams_and_children_alone(chain, phase) -> None:
    chain.ledger.list_derived_tasks = lambda _task_id: [SimpleNamespace(id=_CHILD_TASK_ID)]
    if phase == "running":
        await chain.service.send(_TASK_ID, _send_request("first"))
        await _settle()
        run = chain.ledger.current
    else:
        run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
        run.status = "suspended" if phase == "suspended" else "completed"

    await chain.service.cancel(_TASK_ID)
    if phase == "running":
        await _finish(run.id, "cancelled")

    assert run.status == ("completed" if phase == "idle" else "cancelled")
    assert chain.cancelled_teams == []
    assert chain.child_cancels == []


@pytest.mark.parametrize("phase", ["running", "suspended", "idle"])
async def test_parent_stop_recalls_teams_and_children_at_once(chain, phase) -> None:
    chain.ledger.list_derived_tasks = lambda _task_id: [SimpleNamespace(id=_CHILD_TASK_ID)]
    if phase == "running":
        await chain.service.send(_TASK_ID, _send_request("first"))
        await _settle()
        run = chain.ledger.current
    else:
        run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
        run.status = "suspended" if phase == "suspended" else "completed"

    await chain.actor.cancel("parent")

    # 收回在发起停止时就完成：agent_team 工具阻塞在 Team 上，等 Run 落定会互等。
    assert chain.cancelled_teams == [_TASK_ID]
    assert chain.child_cancels == [(_CHILD_TASK_ID, "parent")]
    if phase == "running":
        await _finish(run.id, "cancelled")
        # 落定后不再重复收回。
        assert chain.cancelled_teams == [_TASK_ID]
        assert chain.child_cancels == [(_CHILD_TASK_ID, "parent")]


def _fail_settling(monkeypatch) -> None:
    def _raise(*_args) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(recall_module, "cancel_placeholders", _raise)


async def test_placeholder_settle_failure_does_not_block_running_cancel(chain, monkeypatch) -> None:
    """收回时占位结算写库失败只记录：信号照常取走、执行照常注销，Task 不会卡在停止中。"""
    _fail_settling(monkeypatch)
    chain.ledger.list_derived_tasks = lambda _task_id: []
    await chain.service.send(_TASK_ID, _send_request("first"))
    await _settle()
    run = chain.ledger.current
    _register_background_work("call-bg", "background", [])

    await chain.actor.cancel("parent")
    await _finish(run.id, "cancelled")

    assert not cancellation_registry.is_cancelled(_TASK_ID)
    assert not chain.actor._execution.driving
    assert run.status == "cancelled"


async def test_placeholder_settle_failure_does_not_block_suspended_cancel(
    chain, monkeypatch
) -> None:
    _fail_settling(monkeypatch)
    chain.ledger.list_derived_tasks = lambda _task_id: []
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"
    _register_background_work("call-bg", "background", [])

    await chain.actor.cancel("parent")

    assert run.status == "cancelled"


# ---------------------------------------------------------------------------
# 取消按信号来源落库结束原因；「用户已停止」由下一次驱动推导，不入队（见 test_runtime_notice）
# ---------------------------------------------------------------------------


async def test_user_cancel_records_its_reason_and_queues_no_notice(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"

    await chain.service.cancel(_TASK_ID)

    assert run.status == "cancelled" and run.end_reason == "user_cancelled"
    assert not any(isinstance(row.body, NoticeBody) for row in chain.queue.rows)


async def test_parent_stop_records_parent_reason(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = "suspended"

    await chain.actor.cancel("parent")

    assert run.status == "cancelled" and run.end_reason == "parent_cancelled"
    assert not any(isinstance(row.body, NoticeBody) for row in chain.queue.rows)


# ---------------------------------------------------------------------------
# 取消不在事件循环线程上同步读库
# ---------------------------------------------------------------------------


async def test_cancel_reads_state_off_the_event_loop(chain) -> None:
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
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
        _TASK_ID,
        TaskMessage.of(
            BackgroundResultBody(
                ended_at="2026-01-01T00:01:00+00:00", tool_call_id="call-probe",
                tool_name="execute_terminal", tool_result="done",
            )
        ),
    )
    await _settle()

    assert len(chain.ledger.runs) == 2


# ---------------------------------------------------------------------------
# 撤回：查执行阶段等停；闸门内交给撤回用例（其语义见 test_turn_withdrawal）；不开新 Run
# ---------------------------------------------------------------------------


class _Context:
    """context 替身：按 sequence 排列的行，软删除只翻 ``include_in_context``。"""

    def __init__(self, *kinds: str) -> None:
        self.rows = [
            SimpleNamespace(
                sequence=sequence,
                message_id=sequence,
                include_in_context=True,
                metadata=SimpleNamespace(kind=kind),
            )
            for sequence, kind in enumerate(kinds, start=1)
        ]

    def included(self) -> list[int]:
        return [row.sequence for row in self.rows if row.include_in_context]


@pytest.fixture
def withdraw(chain, monkeypatch):
    context = _Context("user_input", "assistant", "user_input", "assistant")
    republished: list[int] = []

    def withdraw_turn(task_id: int, message_id: int) -> None:
        """撤回用例替身：移出目标及其后的行（真实语义与目标校验见 test_turn_withdrawal）。"""
        target = next(row for row in context.rows if row.message_id == message_id)
        for row in context.rows:
            if row.sequence >= target.sequence:
                row.include_in_context = False

    monkeypatch.setattr(
        service_module.service_depends,
        "get_turn_withdrawal_service",
        lambda: SimpleNamespace(withdraw=withdraw_turn),
    )
    chain.service._states = SimpleNamespace(
        republish_from_canonical=lambda task_id: republished.append(task_id),
    )
    chain.context, chain.republished = context, republished
    return chain


def _finished_run(chain, status: str = "completed"):
    run = chain.ledger.create_run(_TASK_ID, RunLaunch(model_config_id="test/m1"))
    run.status = status
    return run


async def test_withdraw_removes_the_turn_and_starts_nothing(withdraw) -> None:
    old = _finished_run(withdraw)

    await withdraw.service.withdraw(_TASK_ID, 3)
    await _settle()

    assert withdraw.context.included() == [1, 2]
    assert withdraw.republished == [_TASK_ID]
    # 编辑是前端把内容放回输入框再照常发送：撤回本身不入队、不开新 Run。
    assert withdraw.ledger.current is old and withdraw.queue.rows == []


async def test_withdraw_is_rejected_while_running_without_stopping_the_run(withdraw) -> None:
    """撤回不替用户停止：运行中的 Run 用的是内存上下文，看不到移出，只能拒绝。"""
    await withdraw.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    old = withdraw.ledger.current

    with pytest.raises(actor_module.TaskCommandConflictError) as error:
        await withdraw.service.withdraw(_TASK_ID, 3)

    assert error.value.code == "RUN_NOT_STOPPED"
    assert not cancellation_registry.is_cancelled(_TASK_ID)
    assert old.status == "running" and withdraw.context.included() == [1, 2, 3, 4]
    await _finish(old.id, "completed")


async def test_withdraw_is_rejected_while_suspended(withdraw) -> None:
    old = _finished_run(withdraw, "suspended")

    with pytest.raises(actor_module.TaskCommandConflictError) as error:
        await withdraw.service.withdraw(_TASK_ID, 3)

    assert error.value.code == "RUN_NOT_STOPPED"
    assert old.status == "suspended" and withdraw.context.included() == [1, 2, 3, 4]


async def test_withdraw_is_rejected_while_the_executor_is_still_settling(withdraw) -> None:
    """Run 已落定 cancelled、执行者还在收尾：此时改 context 会与收尾交错，按执行阶段拒绝。"""
    await withdraw.service.send(_TASK_ID, _send_request("hello"))
    await _settle()
    old = withdraw.ledger.current
    _Driver.linger[old.id] = asyncio.Event()
    await _finish(old.id, "cancelled")
    assert old.status == "cancelled"

    with pytest.raises(actor_module.TaskCommandConflictError) as error:
        await withdraw.service.withdraw(_TASK_ID, 3)
    assert error.value.code == "RUN_NOT_STOPPED"
    assert withdraw.context.included() == [1, 2, 3, 4]

    _Driver.linger[old.id].set()
    await _settle()
    await withdraw.service.withdraw(_TASK_ID, 3)
    assert withdraw.context.included() == [1, 2]


async def test_resume_is_refused_once_the_runs_last_turn_was_withdrawn(withdraw) -> None:
    """撤回后这个 Run 没有可续的回合：恢复会把输出接在已撤回的回合上，按不可恢复拒绝。"""
    old = _finished_run(withdraw, "cancelled")
    withdraw.withdrawn.add(old.id)

    with pytest.raises(actor_module.TaskCommandConflictError) as error:
        await withdraw.service.resume(_TASK_ID)

    assert error.value.code == "RUN_NOT_RESUMABLE"
    assert old.status == "cancelled"
