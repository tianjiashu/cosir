"""真实 SQLite 验证外部消息队列的投递与并入、编辑和重启边界。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models import ConversationRunCommand
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_metadata import ToolMessageMetadata
from app.models.task_message import TaskMessage, UserInputBody
from app.service.conversation_run import message_edit
from app.service.conversation_run import task_actor as task_actor_module
from app.service.conversation_run.task_message_queue import TaskMessageQueue
from app.service.conversation_run.user_input_service import UserInputService
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_conversation_task_context_service,
)
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.model.model_config_model import ModelConfigModel
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from tests.sdk_fixtures import sdk_events


class _Inputs:
    """用例口径的队列驱动：按生产路径解析用户输入后入队；用户输入视图带 ``payload``（body）。"""

    def __init__(self) -> None:
        self.queue = TaskMessageQueue()

    def put_user(
        self,
        task_id,
        command,
        model_config_id,
        reasoning_effort=None,
        delivery="immediate",
        agent_id="main_agent",
        replaced_message_id=None,
    ) -> int:
        reuse = None
        if replaced_message_id is not None:
            previous = message_edit.latest_user_input(task_id)
            assert previous is not None
            reuse = previous[1]
        body = UserInputService().prepare(
            task_id, command, model_config_id, reasoning_effort, agent_id, reuse=reuse
        )
        message = TaskMessage.of(body, delivery=delivery)
        if replaced_message_id is None:
            return self.queue.put(task_id, message)
        with main_session_factory().begin() as session:
            message_edit.exclude_from(session, task_id, replaced_message_id)
            return self.queue.put(task_id, message, session=session)

    def pending_users(self, task_id):
        return [
            SimpleNamespace(id=m.id, delivery=m.delivery, payload=m.body)
            for m in self.queue.pending(task_id)
            if isinstance(m.body, UserInputBody)
        ]

    def pending(self, task_id):
        return self.queue.pending(task_id)


def _create_run(service, inputs, task_id):
    """生产口径的交接：配置取队列中最后一条用户输入（``dispatch_policy.next_action``）。"""
    launches = [m.payload.launch for m in inputs.pending_users(task_id)]
    return service.create_run(task_id, launches[-1])


@pytest.fixture
def store(isolated_storage, tmp_path):
    workspace = WorkspaceCrud().create("queue", str(tmp_path))
    task = TaskCrud().create(workspace.id, "queue", tool_schemas=[])
    with main_session_factory().begin() as session:
        config = ModelConfigModel(
            config_name="test",
            base_url="https://example.invalid",
            api_key="test",
            model_name="test",
            context_window_k=32,
        )
        session.add(config)
        session.flush()
        config_id = config.id
    service = get_conversation_run_service()
    inputs = _Inputs()
    inputs.put_user(task.id, ConversationRunCommand(display_text="first"), config_id)
    run = _create_run(service, inputs, task.id)
    return SimpleNamespace(
        task=task,
        config_id=config_id,
        run=run,
        inputs=inputs,
        service=service,
        context=get_conversation_task_context_service(),
        crud=ConversationRunCrud(),
    )


def _open_run(store, command, model_config_id, *, replaced_message_id=None):
    """生产口径的新建 Run：先入队（编辑时同事务移出旧 context），再从 inbox 队首交接。"""
    store.inputs.put_user(
        store.task.id, command, model_config_id, replaced_message_id=replaced_message_id
    )
    return _create_run(store.service, store.inputs, store.task.id)


_SESSIONS: list = []


@pytest.fixture(autouse=True)
async def _close_sessions():
    """用例结束时关闭本模块建的调度入口，actor 循环不跨用例的事件循环残留。"""
    yield
    while _SESSIONS:
        await _SESSIONS.pop().close()


def _session(monkeypatch):
    """返回使用替身执行器的调度入口：只断言启动参数，不真正驱动 workflow。

    替身不登记执行，之后的推进仍按 Run 状态判定（running 视为活动）。
    """
    from unittest.mock import MagicMock

    from app.service.conversation_run.task_actor import TaskActor
    from app.service.conversation_run.workflow_session import WorkflowSession

    start = MagicMock()
    monkeypatch.setattr(
        TaskActor, "_start", lambda _self, run, *, fresh_run: start(run, fresh_run=fresh_run)
    )
    session = WorkflowSession()
    _SESSIONS.append(session)
    return session, SimpleNamespace(start=start)


async def _advance(session, task_id: int, *, by_user: bool):
    """直接按当前事实推进一次（模拟 Run 收尾后的推进），走 actor 的推进实现。"""
    return await session._actor(task_id)._advance(by_user=by_user)


def _starts(executor) -> list[int]:
    """替身执行器收到的启动（run_id）；入口节点由 workflow 按 Run 进度自行推导。"""
    return [call.args[0].id for call in executor.start.call_args_list]


def _delivered_mailbox(store, run):
    """模拟生产投递：Run 首次并入前（新 Run 的初始投递）取全部剩余消息，之后只取 ``immediate``。"""
    from app.service.conversation_run.dispatch_policy import initial_messages

    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    fresh = not any(row.run_id == run.id for row in rows)
    mailbox = RunMailbox()
    for message in initial_messages(store.inputs.pending(store.task.id), fresh_run=fresh):
        mailbox.post(message)
    return mailbox


def _consume(store, run_id):
    """生产口径的一次并入：初始投递后走 prepare_input 的写入事务，返回本次写入 context 的条目。

    manager 以 ``object.__new__`` 构造、跳过系统提示词落库：这里只验证队列确认与消息写入，
    context 行里不应混入系统提示词。
    """
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
    from app.core.workflow.node_helper.common import execution_scope
    from app.core.workflow.nodes.prepare_input_node import _write_messages

    run = store.crud.get(run_id)
    context = object.__new__(RuntimeMessageContextManager)
    context.current_task_id = store.task.id
    context.context_service = store.context
    context.begin_run(run)
    before = len(context._entries)
    with execution_scope(SimpleNamespace(), context):
        _write_messages(_delivered_mailbox(store, run).drain())
    return context._entries[before:]


def _real_context(store, *, run_state=None):
    """真实 Run 状态服务、外部队列、Task 运行空间与 projector 组成的运行上下文；工具系统为空注册表。

    mailbox 已完成初始投递（生产由 ``WorkflowSession`` 在驱动前完成）。

    ``run_state`` 非空时替换节点直接取用的 Run 状态单例（经 ``SINGLETONS``，用例结束后复原）。
    """
    from app.config import configuration
    from app.core.tools.tool_registry import ToolRegistry
    from app.core.workflow.execution_state.runtime_context import RuntimeContext
    from app.service import depends as service_depends
    from tests.runtime_context_stub import SINGLETONS, singleton_getter

    if run_state is not None:
        SINGLETONS.setattr(
            service_depends,
            "get_conversation_run_state_service",
            singleton_getter(lambda: run_state),
        )
    SINGLETONS.setattr(
        configuration,
        "_TOOL_SYSTEM",
        SimpleNamespace(registry=ToolRegistry(), executor=None, user_input_decision_handlers=None),
    )
    run = store.crud.get(store.run.id)
    return RuntimeContext(
        run=run,
        task=store.task,
        workspace=WorkspaceCrud().get(store.task.workspace_id),
        agent=SimpleNamespace(max_steps=5, structured_output=None, model=None),
        allows_tools=frozenset(),
        task_space=task_runtime_spaces.get_or_create(store.task.id),
        event_sink=get_conversation_event_projector().process,
        mailbox=_delivered_mailbox(store, run),
    )


def _drop_pending(store):
    """清掉夹具建 Run 时入队的首条输入，供用例自行构造带附件的首条消息。"""
    from app.storage.crud.task_message_crud import TaskMessageCrud

    with main_session_factory().begin() as session:
        TaskMessageCrud().delete_returning(
            session, [p.id for p in store.inputs.pending_users(store.task.id)]
        )


def _context_manager(store):
    """按运行时装配口径构造 manager：工作区记录供系统提示词与附件解析使用。"""

    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    return RuntimeMessageContextManager(
        store.task.id,
        SimpleNamespace(),
        store.context,
        workspace=SimpleNamespace(id=store.task.workspace_id, root_path=""),
    )


async def _prepare_input(store, context, *, run=None, sink=None):
    """跑真实 prepare_input 节点并入 mailbox 中的消息，返回本次被 manager 接纳的条目。

    ``sink`` 默认交真实 projector，与生产装配一致；用例要断言消费事件时传入收集器。
    """
    from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
    from app.core.workflow.execution_state.react_route import ReactRoute
    from app.core.workflow.node_helper.common import execution_scope
    from app.core.workflow.nodes.prepare_input_node import _prepare_input_node

    rc = _real_context(store)
    rc.run = run or rc.run
    rc.step_limit = 10
    if sink is not None:
        rc.event_sink = sink
    before = len(context._entries)
    with execution_scope(rc, context):
        await _prepare_input_node(
            ReactExecutionState(
                step_count=0, tool_error_count=0, next_node=ReactRoute.PREPARE_INPUT
            )
        )
    return context._entries[before:]


def test_prepare_input_tracks_memory_sequence_and_projects_each_input(store, monkeypatch):
    """prepare_input 一次并入整批已投递输入：内存列表与序号同步推进，每条经 sink 投影一次。"""
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder
    from app.core.events import UserInputConsumedEvent

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    second = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    events: list[object] = []
    context = _context_manager(store)
    context.begin_run(store.run)

    entries = asyncio.run(_prepare_input(store, context, sink=events.append))

    assert [e.message["content"] for e in entries] == ["first", "second"]
    assert [m.get("content") for m in context.load_message()] == ["system", "first", "second"]
    assert all(isinstance(event, UserInputConsumedEvent) for event in events)
    assert [e.message_id for e in events] == [entries[0].message_id, second]
    assert context._message_sequence == entries[-1].sequence + 1
    # 已消费的输入不会被下一次模型边界重复领取。
    assert asyncio.run(_prepare_input(store, context, sink=events.append)) == []
    assert len(events) == 2


def test_edit_needs_no_unload_because_begin_run_rebuilds_memory(store, monkeypatch):
    """编辑重跑不卸载 manager：同一个对象在下一次 begin_run 时从库重建，只看到编辑后的上下文。"""
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run)
    [first] = asyncio.run(_prepare_input(store, context))
    context.add_message({"role": "assistant", "content": "old answer"})
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))

    new_run = _open_run(
        store,
        ConversationRunCommand(display_text="edited"),
        store.config_id,
        replaced_message_id=first.message_id,
    )
    context.begin_run(new_run)
    asyncio.run(_prepare_input(store, context, run=new_run))

    assert [m.get("content") for m in context.load_message()] == ["system", "edited"]
    context.add_message({"role": "assistant", "content": "new answer"})
    sequences = [
        row.sequence
        for row in ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    ]
    assert len(sequences) == len(set(sequences))


def _edit(store, message_id, command):
    """编辑 = 移出旧 context + 新建 Run（生产经 ``WorkflowSession.submit`` 在闸门内调用）。

    返回新 Run 与模型边界消费进 context 的编辑后消息。
    """
    run = _open_run(store, command, store.config_id, replaced_message_id=message_id)
    entries = _consume(store, run.id)
    return run, entries


def test_atomic_queue_consumption_and_multiple_users_same_run(store):
    second = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    pending = store.inputs.pending_users(store.task.id)
    assert [p.payload.message.input_text for p in pending] == ["first", "second"]
    entries = _consume(store, store.run.id)
    assert [e.message.get("content") for e in entries] == ["first", "second"]
    assert entries[-1].message_id == second
    assert all(e.run_id == store.run.id for e in entries)
    assert _consume(store, store.run.id) == []
    assert [e.message_id for e in store.context.entries_in_context(store.task.id)] == [
        p.id for p in pending
    ]


def test_consume_failure_rolls_back_inbox_and_context(store, monkeypatch):
    original = store.context.append

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(store.context, "append", fail)
    with pytest.raises(RuntimeError):
        _consume(store, store.run.id)
    assert len(store.inputs.pending_users(store.task.id)) == 1
    assert store.context.entries_in_context(store.task.id) == []


def test_edit_keeps_prefix_and_opens_new_run(store):
    """编辑 = 改 context + 接收消息：旧 Run 保持终态，编辑后的消息以新身份进入新 Run。"""
    entries = _consume(store, store.run.id)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "first response"}, 2
    )
    mid = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    _consume(store, store.run.id)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "discard"}, 4
    )
    store.crud.update_status_if_in(
        store.run.id,
        "cancelled",
        ("running",),
        execution_data=ConversationRunProgress(step_count=5, tool_error_count=2),
    )
    new_run, [edited] = _edit(store, mid, ConversationRunCommand(display_text="edited"))
    rows = store.context.entries_in_context(store.task.id)
    assert [e.message.get("content") for e in rows] == ["first", "first response", "edited"]
    assert rows[0].message_id == entries[0].message_id
    assert edited.message_id != mid
    assert edited.run_id == new_run.id != store.run.id
    assert new_run.status == "running"
    assert new_run.execution_data == ConversationRunProgress()
    assert store.crud.get(store.run.id).status == "cancelled"
    assert TaskCrud().get(store.task.id).current_run_id == new_run.id


def test_edit_keeps_replaced_history_out_of_context_and_transport(store):
    """编辑不删旧 context：被替换的行留给观测页，但不进模型上下文、不进快照，也不会被再次编辑命中。"""
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

    _consume(store, store.run.id)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "first response"}, 2
    )
    mid = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    _consume(store, store.run.id)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "replaced answer"}, 4
    )
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))

    def edit(message_id: str, text: str) -> str:
        run, [edited] = _edit(store, message_id, ConversationRunCommand(display_text=text))
        store.crud.update_status_if_in(run.id, "cancelled", ("running",))
        return edited.message_id

    once = edit(mid, "edited once")
    twice = edit(once, "edited twice")

    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    by_content = {row.message.get("content"): row for row in rows}
    for replaced in ("second", "replaced answer", "edited once"):
        row = by_content[replaced]
        assert (row.include_in_context, row.project_to_transport) == (False, False)
    latest = by_content["edited twice"]
    assert (latest.include_in_context, latest.project_to_transport) == (True, True)
    assert latest.message_id == twice
    # 被移出的行保留原序号，新消息只追加在最大序号之后。
    assert latest.sequence == max(row.sequence for row in rows)
    assert len({row.sequence for row in rows}) == len(rows)

    in_context = store.context.entries_in_context(store.task.id)
    assert [e.message.get("content") for e in in_context] == [
        "first",
        "first response",
        "edited twice",
    ]
    snapshot = ConversationTaskStateService().rebuild_state(store.task.id)
    snapshot_texts = [
        part.get("text")
        for run in snapshot["runs"]
        for message in run["messages"]
        for part in message["parts"]
        if part.get("type") == "text"
    ]
    # 第一次编辑开出的 Run 已整段移出投影，不留空分组；首个 Run 仍保留未被替换的前缀。
    assert len(snapshot["runs"]) == 2
    assert "replaced answer" not in snapshot_texts
    assert "second" not in snapshot_texts
    assert "edited once" not in snapshot_texts
    assert "edited twice" in snapshot_texts


def test_restart_keeps_suspended_run_and_inbox(store):
    """挂起的 Run 没有执行者：重启收敛不动它，排队消息留给唤醒后的同一个 Run。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="later"), store.config_id
    )
    assert store.service.recover_orphaned_runs() == []
    assert store.crud.get(store.run.id).status == "suspended"
    assert store.inputs.pending_users(store.task.id)[0].payload.message.input_text == "later"


def test_snapshot_groups_multiple_users_and_pending_inputs(store):
    _consume(store, store.run.id)
    store.context.append(store.task.id, store.run.id, {"role": "assistant", "content": "reply"}, 2)
    second = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    _consume(store, store.run.id)
    store.context.append(store.task.id, store.run.id, {"role": "assistant", "content": "reply2"}, 4)
    queued = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="third"), store.config_id
    )
    state = ConversationTaskStateService().rebuild_state(store.task.id)
    assert [m["role"] for m in state["runs"][0]["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # wire 消息 id 是队列行 id 的字符串形式。
    assert state["runs"][0]["messages"][2]["id"] == str(second)
    assert state["queued_messages"][0]["id"] == str(queued)


async def test_completed_run_handoff_claims_next_batch_without_duplicating_input(store, monkeypatch):
    _consume(store, store.run.id)
    message_id = store.inputs.put_user(
        store.task.id, ConversationRunCommand(display_text="next"), store.config_id
    )
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)
    await _advance(session, store.task.id, by_user=False)
    [run_id] = _starts(executor)
    run = store.crud.get(run_id)
    assert run.id != store.run.id
    assert run.status == "running"
    assert [p.id for p in store.inputs.pending_users(store.task.id)] == [message_id]
    entries = _consume(store, run.id)
    assert entries[0].run_id == run.id
    assert entries[0].message_id == message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_send_after_cancel_or_failure_opens_new_run_with_leftover_queue(store, status, monkeypatch):
    """取消或失败后发消息不排队：直接新开 Run，残留的「排队」消息按序一并并入。"""
    _consume(store, store.run.id)
    leftover = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(display_text="later"),
        store.config_id,
        None,
        "next_run",
    )
    store.crud.update_status_if_in(store.run.id, status, ("running",))
    session, executor = _session(monkeypatch)
    receipt = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="now"),
        model_config_id=store.config_id,
        reasoning_effort=None,
        delivery="next_run",
    )
    assert not receipt.queued
    [run_id] = _starts(executor)
    assert run_id != store.run.id
    assert store.crud.get(store.run.id).status == status
    entries = _consume(store, run_id)
    assert [e.message_id for e in entries][0] == leftover
    assert [e.message["content"] for e in entries] == ["later", "now"]


@pytest.mark.asyncio
async def test_send_while_running_queues_by_delivery(store, monkeypatch):
    """运行中发送只进 inbox：模型边界只并入「立即」投递，「排队」投递留给下一个 Run。"""
    session, executor = _session(monkeypatch)
    # 新 Run 的初始投递先完成：此后入队的 next_run 不再进入这个 Run。
    assert [e.message["content"] for e in _consume(store, store.run.id)] == ["first"]
    for text, delivery in (("now", "immediate"), ("later", "next_run")):
        receipt = await session.submit(
            store.task.id,
            ConversationRunCommand(display_text=text),
            model_config_id=store.config_id,
            reasoning_effort=None,
            delivery=delivery,
        )
        assert receipt.queued
    executor.start.assert_not_called()
    entries = _consume(store, store.run.id)
    assert [e.message["content"] for e in entries] == ["now"]
    assert [p.delivery for p in store.inputs.pending_users(store.task.id)] == ["next_run"]


@pytest.mark.asyncio
async def test_unbound_image_token_is_rejected_before_enqueue(store, monkeypatch):
    """正文里没绑定上传图片的图片 token 必须在入队前拒绝。

    它进不了模型正文（图片走 image part），但展示投影要求每个图片 token 都能解析；一旦入队，
    快照重建就会抛错，把 Task 停在「有 running Run 却没有执行者」的状态。
    """
    session, executor = _session(monkeypatch)
    before = [message.id for message in store.inputs.pending(store.task.id)]

    with pytest.raises(UserInputRejectedError) as caught:
        await session.submit(
            store.task.id,
            ConversationRunCommand(display_text="你好 [[cosir-image:abc]]"),
            model_config_id=store.config_id,
            reasoning_effort=None,
        )

    assert caught.value.code == "IMAGE_ATTACHMENT_UNAVAILABLE"
    assert [message.id for message in store.inputs.pending(store.task.id)] == before
    executor.start.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_publish_failure_does_not_orphan_started_run(store, monkeypatch):
    """快照发布是观测旁路：失败只记录，已提交的 Run 仍必须留下执行者。"""
    session, executor = _session(monkeypatch)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    monkeypatch.setattr(
        task_actor_module,
        "_republish",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("projection unavailable")),
    )

    receipt = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="next round"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )

    assert receipt.queued is False
    executor.start.assert_called_once()


@pytest.mark.asyncio
async def test_background_agent_submit_joins_active_run_or_opens_one_with_its_identity(store, monkeypatch):
    """子 Agent / Team 节点与用户发送同一入口：活动时并入当前 Run，终态时按自身身份新开 Run。"""
    session, executor = _session(monkeypatch)
    _consume(store, store.run.id)
    joined = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="follow up"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )
    assert joined.queued is True
    executor.start.assert_not_called()

    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    opened = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="next round"),
        model_config_id=store.config_id,
        reasoning_effort=None,
        agent_id="child_agent",
    )
    assert opened.queued is False
    [opened_run_id] = _starts(executor)
    assert TaskCrud().get(store.task.id).current_run_id == opened_run_id
    assert store.crud.get(opened_run_id).agent_id == "child_agent"


@pytest.mark.asyncio
async def test_edit_requires_stopped_task(store, monkeypatch):
    """编辑只在 Task 已停止时进行：仍有活动 Run 时拒绝，不移出任何 context。"""
    from app.service.conversation_run.workflow_session import SessionConflictError

    [first] = _consume(store, store.run.id)
    with pytest.raises(SessionConflictError) as caught:
        await _session(monkeypatch)[0].rerun(
            store.task.id,
            first.message_id,
            ConversationRunCommand(display_text="edited"),
            model_config_id=store.config_id,
            reasoning_effort=None,
        )
    assert caught.value.code == "RUN_NOT_STOPPED"
    assert [e.message_id for e in store.context.entries_in_context(store.task.id)] == [
        first.message_id
    ]


def test_failed_edit_keeps_original_message(store, monkeypatch):
    """移出旧 context 与入队同一事务：入队失败时原消息仍在上下文与投影里，不丢输入。"""
    [first] = _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    runs_before = store.crud.list_by_task(store.task.id)
    # 失败必须发生在「移出」之后，才验证得到事务回滚；附件/配置问题现在在 prepare 就拦下，
    # 不再能当这里的手段用。
    monkeypatch.setattr(
        TaskMessageQueue,
        "put",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )

    with pytest.raises(RuntimeError):
        _open_run(
            store,
            ConversationRunCommand(display_text="edited"),
            store.config_id,
            replaced_message_id=first.message_id,
        )

    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert [(r.message_id, r.include_in_context, r.project_to_transport) for r in rows] == [
        (first.message_id, True, True)
    ]
    assert store.crud.list_by_task(store.task.id) == runs_before
    assert store.inputs.pending_users(store.task.id) == []


def test_task_input_delivery_rejects_unknown_value(store):
    """delivery 列只接受两种投递方式，持久化边界拒绝其它值。"""
    from sqlalchemy.exc import IntegrityError

    from app.storage.model.task_message_model import TaskMessageModel

    with pytest.raises(IntegrityError):
        with main_session_factory().begin() as session:
            session.add(
                TaskMessageModel(
                    task_id=store.task.id,
                    kind="user_input",
                    payload={},
                    delivery="later",
                    project_to_transport=True,
                )
            )


@pytest.mark.asyncio
async def test_running_task_queues_other_model_for_next_run(store, monkeypatch):
    """运行中换模型：交给下一个 Run 的入队不并入当前 Run、不打断审批，新 Run 用这条配置。"""
    other_id = _other_model()
    session, executor = _session(monkeypatch)
    assert [e.message["content"] for e in _consume(store, store.run.id)] == ["first"]
    receipt = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="now"),
        model_config_id=other_id,
        reasoning_effort=None,
        delivery="next_run",
    )
    assert receipt.queued is True
    assert _consume(store, store.run.id) == []
    assert [p.payload.message.input_text for p in store.inputs.pending_users(store.task.id)] == ["now"]

    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    waiting = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="later"),
        model_config_id=other_id,
        reasoning_effort=None,
        delivery="next_run",
    )
    assert waiting.queued is True
    assert store.crud.get(store.run.id).status == "suspended"
    executor.start.assert_not_called()

    store.crud.update_status_if_in(store.run.id, "cancelled", ("suspended",))
    await _advance(session, store.task.id, by_user=True)
    [run_id] = _starts(executor)
    assert store.crud.get(run_id).model_config_id == other_id


@pytest.mark.asyncio
async def test_queued_edit_keeps_original_launch_and_delivery(store, monkeypatch):
    """排队消息原地编辑只改正文：发起配置与 delivery 沿用发送时的值。"""
    other_id = _other_model()
    session, executor = _session(monkeypatch)
    _consume(store, store.run.id)
    message_id = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(display_text="later", ban_tools=["web_search"]),
        other_id,
        delivery="next_run",
    )

    receipt = await session.edit_queued(
        store.task.id, message_id, ConversationRunCommand(display_text="edited")
    )

    assert receipt.queued is True
    executor.start.assert_not_called()
    [queued] = store.inputs.pending_users(store.task.id)
    assert (queued.id, queued.delivery) == (message_id, "next_run")
    assert queued.payload.message.input_text == "edited"
    assert queued.payload.launch.model_config_id == other_id
    assert queued.payload.launch.options.ban_tools == ["web_search"]


@pytest.mark.asyncio
async def test_queued_edit_of_consumed_message_conflicts_without_rerun(store, monkeypatch):
    """排队编辑命中已并入的消息时 409，不会静默变成「停止当前 Run 并重跑」。"""
    from app.service.conversation_run.workflow_session import SessionConflictError

    [first] = _consume(store, store.run.id)
    session, executor = _session(monkeypatch)

    with pytest.raises(SessionConflictError) as caught:
        await session.edit_queued(
            store.task.id, first.message_id, ConversationRunCommand(display_text="edited")
        )

    assert caught.value.code == "MESSAGE_CONSUMED"
    assert store.crud.get(store.run.id).status == "running"
    executor.start.assert_not_called()
    assert [e.message_id for e in store.context.entries_in_context(store.task.id)] == [
        first.message_id
    ]


@pytest.mark.asyncio
async def test_executor_handoff_waits_for_cleanup_and_starts_new_run(store, monkeypatch):
    """收尾期间入队的输入在旧执行释放后才交给新 Run，两个执行不重叠。"""
    import asyncio

    from app.service.conversation_run.workflow_session import WorkflowSession

    order = []
    finished = asyncio.Event()
    states = get_conversation_run_state_service()
    session = WorkflowSession()
    _SESSIONS.append(session)

    async def drive(run_id, mailbox, initial_delivery):
        await initial_delivery()
        order.append(("start", run_id, session._actors[store.task.id].execution.run.id))
        sequence = store.context.max_sequence(store.task.id) + 1
        _consume(store, run_id)
        if run_id == store.run.id:
            store.inputs.put_user(
                store.task.id,
                ConversationRunCommand(display_text="during final"),
                store.config_id,
            )
        states.transition(run_id, RunTransition.COMPLETE, final_output="done")
        if run_id != store.run.id:
            finished.set()

    monkeypatch.setattr(task_actor_module, "drive_run", drive)
    # store 已建好 running Run：直接让 actor 接管它的执行（生产由推进新建后启动）。
    actor = session._actor(store.task.id)
    actor._start(store.crud.get(store.run.id), fresh_run=True)
    actor.execution.ready.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert await session.wait_idle(store.task.id, 2) is True
    assert order[0] == ("start", store.run.id, store.run.id)
    # 新 Run 启动时执行登记里只有它自己：旧执行已在 RunFinished 收尾时注销。
    assert order[1][0] == "start" and order[1][1] != store.run.id and order[1][2] == order[1][1]
    assert store.inputs.pending_users(store.task.id) == []
    assert actor.execution is None


@pytest.mark.asyncio
async def test_completed_model_response_leaves_during_stream_input_for_next_run(store, monkeypatch):
    from unittest.mock import AsyncMock

    import app.core.workflow.workflow as workflow_module
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder

    states = get_conversation_run_state_service()
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    requests = []

    async def stream(messages, **kwargs):
        requests.append([m.get("content") for m in messages if m["role"] == "user"])
        text = "during request" if len(requests) == 1 else "after final"
        store.inputs.put_user(
            store.task.id, ConversationRunCommand(display_text=text), store.config_id
        )
        for event in sdk_events(
            [{"content": "partial" if len(requests) == 1 else "done"}],
            finish_reason="length" if len(requests) == 1 else "stop",
        ):
            yield event
            persisted = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
            assert sum(row.message["role"] == "assistant" for row in persisted) == len(requests)

    model = SimpleNamespace(stream=stream, close=AsyncMock())
    monkeypatch.setattr(
        workflow_module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=False),
    )
    ops = _real_context(store)
    await workflow_module.ReactLikeWorkflow().run(ops)
    assert requests == [["first"]]
    responses = [
        row
        for row in store.context.entries_in_context(store.task.id)
        if row.message["role"] == "assistant"
    ]
    assert [row.message["content"] for row in responses] == ["partial"]
    assert store.crud.get(store.run.id).status == "completed"
    assert [p.payload.message.input_text for p in store.inputs.pending_users(store.task.id)] == [
        "during request"
    ]
    messages = ConversationTaskStateService().rebuild_state(store.task.id)["runs"][0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_queue_ack_does_not_start_another_executor(store, monkeypatch):
    from app.assistant_transport.assistant_api import send_message
    from app.assistant_transport.request import SendMessageRequest

    session, executor = _session(monkeypatch)
    accepted = await send_message(
        store.task.id,
        SendMessageRequest.model_validate(
            {
                "message": {"role": "user", "parts": [{"type": "text", "text": "second"}]},
                "modelConfigId": store.config_id,
            }
        ),
        session=session,
        state_service=ConversationTaskStateService(),
    )
    assert accepted.queued is True
    assert len(accepted.state["queued_messages"]) == 2
    executor.start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_queue_commands_during_stream_preserve_output_and_replace_one_row(
    store, monkeypatch, buffered
):
    """真实 loop/SDK 聚合/SQLite/API：queue 不重建输出，待发 delta 不丢失、不重复。"""
    from unittest.mock import AsyncMock

    import app.core.workflow.workflow as workflow_module
    from app.assistant_transport.assistant_api import send_message
    from app.assistant_transport.request import SendMessageRequest
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )
    from app.config.constant import Constant
    from app.core.context.runtime_message_context_manager import (
        SystemPromptBuilder,
    )

    states = get_conversation_run_state_service()
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MIN_CHARS", 8 if buffered else 0)
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS", 3600)
    state_service = ConversationTaskStateService()
    session, executor = _session(monkeypatch)
    identity = None
    first = "abcdefgh"

    def parts(state):
        return state["runs"][0]["messages"][-1]["parts"]

    async def stream(messages):
        nonlocal identity
        for index, event in enumerate(
            sdk_events(
                [
                    {"reasoning_content": "thinking"},
                    {"content": first},
                    {"content": "B"},
                    {"content": " tail"},
                ]
            )
        ):
            yield event
            rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
            responses = [row for row in rows if row.message["role"] == "assistant"]
            assert len(responses) == 1
            row = responses[0]
            if identity is None:
                identity = (row.id, row.message_id, row.sequence)
            assert (row.id, row.message_id, row.sequence) == identity
            assert not row.include_in_context
            if index != 2:
                continue
            assert row.message["content"] == first + "B"
            # 直接从数据库重建能恢复最新 SDK 输出；不替换仍有待发 delta 的活快照。
            cold = ConversationTaskStateRebuilder.rebuild(
                TaskCrud().get(store.task.id), [store.crud.get(store.run.id)], rows
            )
            assert [p["text"] for p in parts(cold)] == ["thinking", first + "B"]
            reloaded = _context_manager(store)
            reloaded.begin_run(store.run)
            assert [m["role"] for m in reloaded.load_message()] == ["system", "user"]
            before = state_service.get_state(store.task.id)
            assert [p["text"] for p in parts(before)] == [
                "thinking",
                first if buffered else first + "B",
            ]
            accepted = await send_message(
                store.task.id,
                SendMessageRequest.model_validate(
                    {
                        "message": {"role": "user", "parts": [{"type": "text", "text": "second"}]},
                        "modelConfigId": store.config_id,
                    }
                ),
                session=session,
                state_service=state_service,
            )
            assert accepted.queued
            assert accepted.state["runs"] == before["runs"]
            assert accepted.state["queued_messages"][0]["parts"][0]["text"] == "second"
            assert state_service.get_state(store.task.id)["runs"] == before["runs"]

    model = SimpleNamespace(stream=stream, close=AsyncMock())
    monkeypatch.setattr(
        workflow_module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=True),
    )
    operations = _real_context(store)
    await workflow_module.ReactLikeWorkflow().run(operations)
    live = state_service.get_state(store.task.id)
    assert [p["text"] for p in parts(live)] == ["thinking", first + "B tail"]
    assert all(p["status"] == "completed" for p in parts(live))
    assert state_service.rebuild_state(store.task.id) == live
    row = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)[-1]
    assert (row.id, row.message_id, row.sequence) == identity
    assert row.include_in_context
    assert row.message["content"] == first + "B tail"
    assert len(store.inputs.pending_users(store.task.id)) == 1
    executor.start.assert_not_called()


@pytest.mark.asyncio
async def test_immediate_send_wakes_the_suspended_run(store, monkeypatch):
    """挂起中收到立即发送：唤醒同一个 Run（不新建），消息随续跑的初始投递并入。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    session, executor = _session(monkeypatch)
    receipt = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="continue"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )
    assert receipt.queued is False
    assert _starts(executor) == [store.run.id]
    assert store.crud.get(store.run.id).status == "running"
    assert executor.start.call_args.kwargs == {"fresh_run": False}


@pytest.mark.asyncio
async def test_cancel_suspended_run_settles_directly_and_leaves_user_notice(store, monkeypatch):
    """挂起中取消：没有执行者，闸门内直接落定 cancelled，并给下一个 Run 留「用户已取消」通知。"""
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    session, executor = _session(monkeypatch)

    assert await session.cancel(store.task.id) is True

    assert store.crud.get(store.run.id).status == "cancelled"
    executor.start.assert_not_called()
    [notice] = store.inputs.pending(store.task.id)
    assert (notice.body.kind, notice.delivery) == ("notice", "next_run")
    assert notice.body.notice_kind == "user_cancelled"


@pytest.mark.asyncio
async def test_edit_cancel_of_suspended_run_leaves_no_notice(store, monkeypatch):
    """运行中编辑停止挂起的 Run：不留「用户已取消」通知，编辑后的消息开出新 Run。"""
    [first] = _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    session, executor = _session(monkeypatch)

    await session.rerun(
        store.task.id,
        first.message_id,
        ConversationRunCommand(display_text="edited"),
        model_config_id=store.config_id,
        reasoning_effort=None,
    )

    assert store.crud.get(store.run.id).status == "cancelled"
    [run_id] = _starts(executor)
    assert run_id != store.run.id
    assert [m.body.kind for m in store.inputs.pending(store.task.id)] == ["user_input"]


@pytest.mark.asyncio
async def test_failed_resume_updates_existing_snapshot_without_consuming_queue(store, monkeypatch):
    store.crud.update_status_if_in(store.run.id, "failed", ("running",))
    state = ConversationTaskStateService()
    assert state.rebuild_state(store.task.id)["runs"][0]["status"] == "failed"
    session, executor = _session(monkeypatch)
    await session.resume(store.task.id)
    assert _starts(executor) == [store.run.id]
    assert state.get_state(store.task.id)["runs"][0]["status"] == "running"
    assert len(store.inputs.pending_users(store.task.id)) == 1


def test_sdk_context_roundtrip_keeps_identity_metadata_and_raw_arguments(store, monkeypatch):
    """数据库读回 SDK JSON；消息身份、用户展示和响应信息均不进入模型正文。"""
    import json

    from app.core.context.runtime_message_context_manager import (
        SystemPromptBuilder,
    )

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run)
    asyncio.run(_prepare_input(store, context))
    user = store.context.entries_in_context(store.task.id)[1]
    assert user.metadata.presentation.display_text == "first"
    assert "input_text" not in user.metadata.presentation.model_dump()
    assert user.message == {"role": "user", "content": "first"}
    assert user.message_id
    for _event in sdk_events(
        [
            {
                "content": "checking",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
        ],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
    ):
        response_entry = context.save_model_response(
            _event.snapshot, include_in_context=False, registered_tool_names={"read_file"}
        )
        rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
        responses = [row for row in rows if row.message["role"] == "assistant"]
        assert len(responses) == 1
        assert responses[0].message_id == response_entry.message_id
        assert not responses[0].include_in_context
    context.save_model_response(
        _event.snapshot, include_in_context=True, registered_tool_names={"read_file"}
    )
    finalized = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)[-1]
    assert finalized.metadata.finish_reason == "tool_calls"
    assert finalized.metadata.usage.total_tokens == 10
    assert finalized.message["tool_calls"][0]["function"]["arguments"] == '{"path":"a.py"}'
    assert "parsed" not in json.dumps(finalized.message)
    context.add_message(
        {"role": "tool", "tool_call_id": "call", "content": "waiting"},
        metadata=ToolMessageMetadata.model_validate({"status": "running"}),
    )
    waiting = store.context.entries_in_context(store.task.id)[-1]
    context.add_message(
        {"role": "tool", "tool_call_id": "call", "content": "file contents"},
        metadata=ToolMessageMetadata.model_validate({"status": "completed"}),
    )
    completed = store.context.entries_in_context(store.task.id)[-1]
    assert (completed.message_id, completed.sequence) == (waiting.message_id, waiting.sequence)
    reloaded = _context_manager(store)
    reloaded.begin_run(store.run)
    assert reloaded.load_message() == context.load_message()
    assert [m["role"] for m in reloaded.load_message()] == ["system", "user", "assistant", "tool"]
    assert all(
        "user_input" not in m and "message_id" not in m and "usage" not in m
        for m in reloaded.load_message()
    )


@pytest.mark.asyncio
async def test_queue_during_tool_preview_keeps_pending_part_executable(store, monkeypatch):
    """已落库的半截工具调用不能因 queue 冷重建而把实时 pending 预览误标 cancelled。"""

    from app.assistant_transport.assistant_api import send_message
    from app.assistant_transport.request import SendMessageRequest
    from app.core.context.runtime_message_context_manager import (
        SystemPromptBuilder,
    )
    from app.core.events import ToolCallCreatedEvent, ToolCallStatusChangedEvent
    from tests.sdk_fixtures import sdk_completion

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run)
    await _prepare_input(store, context)
    state_service = ConversationTaskStateService()
    context.save_model_response(
        sdk_completion(
            tool_calls=[
                {
                    "id": "call-preview",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }
            ]
        ),
        include_in_context=False,
        registered_tool_names={"read_file"},
    )
    state_service.apply_planned(
        ToolCallCreatedEvent(
            task_id=store.task.id,
            run_id=store.run.id,
            tool_call_id="call-preview",
            tool_name="read_file",
        )
    )
    before = state_service.get_state(store.task.id)
    session, executor = _session(monkeypatch)
    accepted = await send_message(
        store.task.id,
        SendMessageRequest.model_validate(
            {
                "message": {"role": "user", "parts": [{"type": "text", "text": "second"}]},
                "modelConfigId": store.config_id,
            }
        ),
        session=session,
        state_service=state_service,
    )
    assert accepted.state["runs"] == before["runs"]
    assert before["runs"][0]["messages"][-1]["parts"][-1]["status"] == "pending"
    state_service.apply_planned(
        ToolCallStatusChangedEvent(
            task_id=store.task.id,
            run_id=store.run.id,
            tool_call_id="call-preview",
            status="running",
            args={"path": "a.py"},
        )
    )
    state = state_service.get_state(store.task.id)
    assert state["runs"][0]["messages"][-1]["parts"][-1]["status"] == "running"
    executor.start.assert_not_called()


def test_cancelled_sdk_output_is_durable_but_excluded_on_resume(store, monkeypatch):
    from app.core.context.runtime_message_context_manager import (
        SystemPromptBuilder,
    )

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run)
    asyncio.run(_prepare_input(store, context))
    events = sdk_events([{"content": "partial"}, {"content": " tail"}], finish_reason=None)
    last_event = list(events)[-1]
    context.save_model_response(
        last_event.snapshot, include_in_context=False, registered_tool_names=set()
    )
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].message["content"] == "partial tail"
    assert not rows[-1].include_in_context
    reloaded = _context_manager(store)
    reloaded.begin_run(store.run)
    assert [m["role"] for m in reloaded.load_message()] == ["system", "user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("boundary", ["chunk", "end", "timeout"])
async def test_cancel_or_failure_closes_before_resume_or_edit(store, monkeypatch, edit, boundary):
    """真实 Runtime/loop/SQLite：超时仍失败，取消或失败草稿均不进入续跑与编辑上下文。"""
    import asyncio
    from unittest.mock import AsyncMock

    import app.core.workflow.workflow as workflow_module
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder
    from app.core.hook import HookInterceptor
    from app.core.workflow.cancel.conversation_run_cancellation_registry import (
        cancellation_registry,
    )
    from app.service.conversation_run import run_driver

    states = get_conversation_run_state_service()
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    reading = asyncio.Event()
    closed = asyncio.Event()
    resume_read = asyncio.Event()
    requests = []

    async def stream(messages, **kwargs):
        requests.append(messages)
        if len(requests) == 1:
            try:
                for event in sdk_events([{"content": "partial"}], finish_reason=None):
                    yield event
                rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
                responses = [row for row in rows if row.message["role"] == "assistant"]
                assert len(responses) == 1
                assert responses[0].message["content"] == "partial"
                assert not responses[0].include_in_context
                reading.set()
                await resume_read.wait()
                if boundary == "timeout":
                    raise TimeoutError("SDK read timed out")
                if boundary == "chunk":
                    for event in sdk_events([{"content": "ignored after cancel"}]):
                        yield event
            finally:
                closed.set()
        else:
            assert closed.is_set()
            for event in sdk_events([{"content": "done"}], finish_reason="stop"):
                yield event

    model = SimpleNamespace(stream=stream, close=AsyncMock())
    monkeypatch.setattr(
        workflow_module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=False),
    )

    def cancel_run(**kwargs):
        assert closed.is_set()
        return states.transition(store.run.id, RunTransition.CANCEL, **kwargs)

    run_state = SimpleNamespace(
        transition=lambda run_id, transition, **kw: (
            cancel_run(**kw)
            if transition is RunTransition.CANCEL
            else states.transition(run_id, transition, **kw)
        ),
        save_execution=states.save_execution,
        update_completed_result=states.update_completed_result,
    )
    monkeypatch.setattr(run_driver, "_runtime_config", lambda *_: SimpleNamespace())
    # 每次驱动现读 Run：同一用例先驱动、再续跑同一 Run。
    monkeypatch.setattr(
        run_driver, "_build_context", lambda *_: _real_context(store, run_state=run_state)
    )
    monkeypatch.setattr(HookInterceptor, "async_safe_fire", AsyncMock())
    original_transition = states.transition

    def transition(run_id, run_transition, **kwargs):
        if run_transition is RunTransition.FAIL:
            assert closed.is_set()
            state = ConversationTaskStateService().get_state(store.task.id)
            parts = state["runs"][0]["messages"][-1]["parts"]
            assert parts and all(part["status"] == "completed" for part in parts)
        return original_transition(run_id, run_transition, **kwargs)

    monkeypatch.setattr(states, "transition", transition)
    cancellation_registry.consume(store.task.id)
    def drive(run_id):
        """按调度方的初始投递驱动：把外部队列中可投递的消息放进本次 mailbox。"""
        mailbox = RunMailbox()

        async def deliver() -> None:
            for message in store.inputs.pending(store.task.id):
                if message.delivery == "immediate":
                    mailbox.post(message)

        return run_driver.drive_run(run_id, mailbox, deliver)

    driver = asyncio.create_task(drive(store.run.id))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        if not edit:
            store.inputs.put_user(
                store.task.id, ConversationRunCommand(display_text="next"), store.config_id
            )
        cancellation_registry.request_cancel(store.task.id)
        assert not driver.done()
        assert not closed.is_set()
        assert store.crud.get(store.run.id).status == "running"
        resume_read.set()
        await asyncio.wait_for(driver, 1)
        assert store.crud.get(store.run.id).status == (
            "failed" if boundary == "timeout" else "cancelled"
        )
        rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
        draft = next(row for row in rows if row.message.get("content") == "partial")
        assert not draft.include_in_context
        user = next(row for row in rows if row.message["role"] == "user")
        if edit:
            # 编辑开出新 Run；后续驱动、上下文装配与断言都转向它（旧 Run 保持终态）。
            store.run = _open_run(
                store,
                ConversationRunCommand(display_text="edited"),
                store.config_id,
                replaced_message_id=user.message_id,
            )
        else:
            assert [p.payload.message.input_text for p in store.inputs.pending_users(store.task.id)] == [
                "next"
            ]
            states.transition(store.run.id, RunTransition.RESUME)
        cancellation_registry.consume(store.task.id)
        await drive(store.run.id)
        assert store.crud.get(store.run.id).status == "completed"
        assert all(m.get("content") != "partial" for m in requests[1])
        assert [m["content"] for m in requests[1] if m["role"] == "user"] == (
            ["edited"] if edit else ["first", "next"]
        )
        assert store.inputs.pending_users(store.task.id) == []
        assert model.close.await_count == 2
    finally:
        if not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        cancellation_registry.consume(store.task.id)


async def test_queued_options_only_apply_when_handing_off_to_new_run(store, monkeypatch):
    """交给下一个 Run 的输入携带的配置只在交接时生效，当前 Run 配置不变。"""
    assert [e.message["content"] for e in _consume(store, store.run.id)] == ["first"]
    next_id = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(display_text="after final", ban_tools=["read_file"]),
        store.config_id,
        None,
        "next_run",
    )
    assert _consume(store, store.run.id) == []
    assert store.crud.get(store.run.id).options.ban_tools == []
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)
    await _advance(session, store.task.id, by_user=False)
    [run_id] = _starts(executor)
    run = store.crud.get(run_id)
    assert run.options.ban_tools == ["read_file"]
    transferred = _consume(store, run.id)
    assert len(transferred) == 1
    assert transferred[0].run_id == run.id
    assert transferred[0].message_id == next_id


def test_consumed_edit_uses_its_own_attachment_and_keeps_run_options(store, tmp_path):
    """多输入 Run 中编辑第二条消息，只复用该消息附件，不从第一条或 Run 取值。"""
    from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput

    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first")
    second_path.write_text("second")
    _drop_pending(store)
    first_id = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(
            display_text="first [[cosir-file:file]]",
            attachments=[
                ConversationRunAttachmentInput(
                    id="file",
                    name="first.txt",
                    content_type="text/plain",
                    path=str(first_path),
                )
            ],
        ),
        store.config_id,
    )
    _consume(store, store.run.id)
    second_id = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(
            display_text="second [[cosir-file:file]]",
            attachments=[
                ConversationRunAttachmentInput(
                    id="file",
                    name="second.txt",
                    content_type="text/plain",
                    path=str(second_path),
                )
            ],
        ),
        store.config_id,
    )
    _consume(store, store.run.id)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "discard"}, 3
    )
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    new_run, [edited] = _edit(
        store,
        second_id,
        ConversationRunCommand(display_text="edited [[cosir-file:file]]", ban_tools=["write_file"]),
    )
    rows = store.context.entries_in_context(store.task.id)
    assert [row.message_id for row in rows] == [first_id, edited.message_id]
    assert rows[0].message["content"] == f"first {first_path}"
    assert rows[1].message["content"] == f"edited {second_path}"
    assert rows[1].metadata.presentation.attachments[0]["path"] == str(second_path)
    # 编辑开出的新 Run 采用本次输入的配置；旧 Run 的配置不变。
    assert new_run.options.ban_tools == ["write_file"]
    assert store.crud.get(store.run.id).options.ban_tools == []


def test_progress_and_run_options_reject_legacy_payloads_at_storage_boundary(store):
    """旧形状直接失败；不从旧输入字段推导配置，也不静默清空未知等待数据。"""
    from pydantic import ValidationError
    from sqlalchemy import update

    from app.storage.model.conversation_run_model import ConversationRunModel

    assert {"input_text", "image_paths", "extra"}.isdisjoint(
        ConversationRunModel.__table__.columns.keys()
    )
    with main_session_factory().begin() as session:
        session.execute(
            update(ConversationRunModel)
            .where(ConversationRunModel.id == store.run.id)
            .values(
                options={"display_text": "old input", "ban_tools": []},
            )
        )
    with pytest.raises(ValidationError, match="display_text"):
        store.crud.get(store.run.id)
    with main_session_factory().begin() as session:
        session.execute(
            update(ConversationRunModel)
            .where(ConversationRunModel.id == store.run.id)
            .values(
                options={"ban_tools": [], "reasoning_effort": None},
                execution_data={"waiting_state": {}},
            )
        )
    with pytest.raises(ValidationError, match="waiting_state"):
        store.crud.get(store.run.id)


def test_edit_can_remove_an_attachment_deleted_since_original_message(store, tmp_path):
    from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput

    attachment = tmp_path / "removed.txt"
    attachment.write_text("content")
    _drop_pending(store)
    message_id = store.inputs.put_user(
        store.task.id,
        ConversationRunCommand(
            display_text="read [[cosir-file:removed]]",
            attachments=[
                ConversationRunAttachmentInput(
                    id="removed",
                    name="removed.txt",
                    content_type="text/plain",
                    path=str(attachment),
                )
            ],
        ),
        store.config_id,
    )
    _consume(store, store.run.id)
    attachment.unlink()
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    _edit(store, message_id, ConversationRunCommand(display_text="ignore that file"))
    row = store.context.entries_in_context(store.task.id)[0]
    assert row.message == {"role": "user", "content": "ignore that file"}
    assert row.metadata.presentation.attachments == []


@pytest.mark.asyncio
async def test_concurrent_sends_to_idle_task_open_one_run_and_queue_the_other(store, monkeypatch):
    """actor 串行化并发发送：只新开一个 Run，另一条消息进入该 Run 的 inbox。"""
    import asyncio

    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    session, executor = _session(monkeypatch)
    receipts = await asyncio.gather(
        *(
            session.submit(
                store.task.id,
                ConversationRunCommand(display_text=text),
                model_config_id=store.config_id,
                reasoning_effort=None,
                delivery="immediate",
            )
            for text in ("one", "two")
        )
    )
    # 落在同一批时合并成一次推进（两条都 False）；分两批时后一条进新 Run 的 inbox（True）。
    assert receipts[0].queued is False or receipts[1].queued is False
    assert len(_starts(executor)) == 1
    assert len(store.crud.list_by_task(store.task.id)) == 2


def _other_model() -> int:
    """再建一份模型配置，模拟用户在 Run 进行中切换模型。"""
    with main_session_factory().begin() as session:
        other = ModelConfigModel(
            config_name="other",
            base_url="https://example.invalid",
            api_key="test",
            model_name="other",
            context_window_k=32,
        )
        session.add(other)
        session.flush()
        return other.id


def test_prepare_input_leaves_queue_when_cancelled(store, monkeypatch):
    """取消后 prepare_input 不取 mailbox，直接落定 cancelled 并结束驱动；排队消息留在外部队列。"""
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder
    from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
    from app.core.workflow.execution_state.react_route import ReactRoute
    from app.core.workflow.node_helper.common import RunStopped, execution_scope
    from app.core.workflow.nodes.prepare_input_node import _prepare_input_node

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run)
    rc = _real_context(store)
    rc.step_limit = 10
    monkeypatch.setattr(type(rc), "is_cancelled", lambda _self: True)
    with execution_scope(rc, context), pytest.raises(RunStopped):
        asyncio.run(
            _prepare_input_node(
                ReactExecutionState(
                    step_count=0, tool_error_count=0, next_node=ReactRoute.PREPARE_INPUT
                )
            )
        )

    assert store.crud.get(store.run.id).status == "cancelled"
    assert len(store.inputs.pending_users(store.task.id)) == 1


@pytest.mark.asyncio
async def test_wait_idle_requires_no_executor_and_terminal_latest_run(store, monkeypatch):
    """静止 = 无执行者且最新 Run 已到终态；运行中与等待审批都不算静止。"""
    session, _ = _session(monkeypatch)
    assert await session.wait_idle(store.task.id, 0.05) is False

    store.crud.update_status_if_in(store.run.id, "suspended", ("running",))
    assert await session.wait_idle(store.task.id, 0.05) is False

    store.crud.update_status_if_in(store.run.id, "completed", ("suspended",))
    assert await session.wait_idle(store.task.id, 0.05) is True


@pytest.mark.asyncio
async def test_wait_idle_wakes_when_execution_releases(store, monkeypatch):
    """执行收尾即唤醒等待方，不必等到超时；判定按 Task，与等的是哪条 Run 无关。"""
    import time

    from app.service.conversation_run.task_actor import Execution
    from app.service.conversation_run.task_signals import RunFinished

    session, _ = _session(monkeypatch)
    _consume(store, store.run.id)
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    actor = session._actor(store.task.id)
    execution = Execution(run=store.crud.get(store.run.id), fresh_run=False)
    execution.thread_task = asyncio.create_task(asyncio.sleep(0))
    actor.execution = execution

    async def finish() -> None:
        await asyncio.sleep(0.05)
        actor.send(RunFinished(execution))

    asyncio.create_task(finish())
    started = time.monotonic()
    assert await session.wait_idle(store.task.id, 5) is True
    assert time.monotonic() - started < 1
