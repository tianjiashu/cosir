"""TaskActor 的串行调度语义（真实 SQLite）：批量合并、取消不被慢处理阻塞、编辑重跑两阶段、
删除前停止并退出。设计见 ``docs/plan/task-actor-dispatch-design.md``。
"""

import asyncio

import pytest

from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunCommand
from app.models.enums.conversation_run_status import RunTransition
from app.models.task_message import TaskMessage
from app.service.conversation_run import task_actor as task_actor_module
from app.service.conversation_run.task_actors import TaskActors
from app.service.conversation_run.task_signals import Enqueue
from app.service.conversation_run.user_input_service import UserInputService
from app.service.depends import get_conversation_run_state_service
from tests import test_task_input_queue as queue_tests
from tests.test_task_input_queue import _SESSIONS, _consume, _session, _starts

# 复用队列用例的真实 SQLite fixture 与会话清理。
store = queue_tests.store
_close_sessions = queue_tests._close_sessions


@pytest.fixture(autouse=True)
def _clear_signal(store):
    yield
    cancellation_registry.consume(store.task.id)


def _real_session(store, monkeypatch, started: list[int]) -> TaskActors:
    """真实调度入口；store 已有的 running Run 交给 actor 执行（生产由推进新建后启动）。"""
    task_id = store.task.id

    async def drive(run_id, _mailbox, initial_delivery):
        await initial_delivery()
        started.append(run_id)
        # 取消信号注册表没有可 await 的事件：轮询模拟检查点。
        while not cancellation_registry.is_cancelled(task_id):  # noqa: ASYNC110
            await asyncio.sleep(0.005)
        get_conversation_run_state_service().transition(run_id, RunTransition.CANCEL)

    monkeypatch.setattr(task_actor_module, "drive_run", drive)
    session = TaskActors()
    _SESSIONS.append(session)
    actor = session._actor(task_id)
    actor._start(store.crud.get(store.run.id), fresh_run=False)
    actor.execution.ready.set()
    return session


def _input(store, text: str) -> TaskMessage:
    body = UserInputService().prepare(
        store.task.id,
        ConversationRunCommand(display_text=text),
        store.config_id,
        None,
        "main_agent",
    )
    return TaskMessage.of(body)


async def test_enqueue_signals_in_one_batch_advance_once(store, monkeypatch):
    """同一批的入队信号合并成一次推进：只开一个 Run。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)
    actor = session._actor(store.task.id)
    advanced: list[bool] = []
    original = actor._advance

    async def counting(*, by_user: bool, project: bool = False):
        advanced.append(by_user)
        return await original(by_user=by_user, project=project)

    monkeypatch.setattr(actor, "_advance", counting)
    changed = actor.state_changed
    # 循环尚未取过信号：两条在同一批。
    for text in ("one", "two"):
        actor.send(Enqueue(_input(store, text), by_user=True, admit=False))

    await asyncio.wait_for(changed.wait(), 2)

    assert advanced == [True]
    assert len(_starts(executor)) == 1


async def test_cancel_is_not_blocked_by_slow_signal_handling(store, monkeypatch):
    """actor 正在慢处理别的信号时，运行中的取消仍在入口同步生效，不排在它后面。"""
    _consume(store, store.run.id)
    started: list[int] = []
    session = _real_session(store, monkeypatch, started)
    actor = session._actors[store.task.id]
    release = asyncio.Event()
    original = actor._enqueue

    async def slow(signal, merged):
        await release.wait()
        await original(signal, merged)

    monkeypatch.setattr(actor, "_enqueue", slow)
    submitting = asyncio.create_task(
        session.submit(
            store.task.id,
            ConversationRunCommand(display_text="later"),
            model_config_id=store.config_id,
            reasoning_effort=None,
        )
    )
    await asyncio.sleep(0.05)

    session.cancel(store.task.id)
    assert cancellation_registry.is_cancelled(store.task.id) is True
    release.set()
    await asyncio.wait_for(submitting, 2)


async def test_edit_running_run_stops_then_reruns_in_two_phases(store, monkeypatch):
    """运行中编辑：先以 edited 停止当前 Run，收尾后移出旧输入并以新消息开新 Run；不留取消通知。"""
    [first] = _consume(store, store.run.id)
    started: list[int] = []
    session = _real_session(store, monkeypatch, started)

    await session.rerun(
        store.task.id,
        first.message_id,
        ConversationRunCommand(display_text="edited"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )
    # 没有回执：等两阶段走完（当前 Run 停下 → 收尾时完成编辑并开出新 Run）。
    actor = session._actors[store.task.id]
    while len(store.crud.list_by_task(store.task.id)) < 2:
        await asyncio.wait_for(actor.state_changed.wait(), 2)

    assert store.crud.get(store.run.id).status == "cancelled"
    runs = store.crud.list_by_task(store.task.id)
    assert len(runs) == 2
    [pending] = store.inputs.pending(store.task.id)
    assert pending.body.kind == "user_input"
    assert pending.body.message.input_text == "edited"
    assert store.context.entries_in_context(store.task.id) == []


async def test_stop_tasks_stops_execution_and_evicts_actor(store, monkeypatch):
    """删除前停止：按上游停止落定 cancelled（不留用户取消通知），actor 停下后退出。"""
    _consume(store, store.run.id)
    started: list[int] = []
    session = _real_session(store, monkeypatch, started)

    await asyncio.wait_for(session.stop_tasks([store.task.id]), 2)

    assert store.crud.get(store.run.id).status == "cancelled"
    assert store.task.id not in session._actors
    assert store.inputs.pending(store.task.id) == []


async def test_deliver_then_wait_new_run_returns_started_run(store, monkeypatch):
    """内部投递只等解析与发信号；投递方随后订阅到本次新开的 Run（Team 节点据此记录节点 Run）。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)

    await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="node input"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )
    run_id = await asyncio.wait_for(session.wait_new_run(store.task.id, store.run.id, 2), 3)

    assert run_id is not None and run_id != store.run.id
    assert _starts(executor) == [run_id]


async def test_wait_new_run_raises_when_delivery_starts_no_run(store, monkeypatch):
    """投递已处理而 Task 静止、仍没有新 Run：启动失败要上浮，不能让等待方一直等。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, _executor = _session(monkeypatch)
    actor = session._actor(store.task.id)

    def broken_create_run(*_args, **_kwargs):
        raise RuntimeError("model config deleted")

    monkeypatch.setattr(actor._runs, "create_run", broken_create_run)
    await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="node input"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.wait_new_run(store.task.id, store.run.id, 2), 3)


async def test_stop_tasks_of_missing_task_does_not_hang(store, monkeypatch):
    """停止失败（Task 已不存在）只在 actor 内记录，actor 照样退出，删除流程不会卡住。"""
    session, _executor = _session(monkeypatch)

    await asyncio.wait_for(session.stop_tasks([store.task.id + 10_000]), 2)

    assert store.task.id + 10_000 not in session._actors
