
"""真实 SQLite 验证 inbox 转移、编辑和重启边界。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.models import ConversationRunCommand
from app.models.conversation_run_progress import (
    ConversationRunProgress,
    ToolContinuation,
)
from app.models.enums.conversation_run_status import RunTransition
from app.models.message_metadata import ToolMessageMetadata
from app.service.conversation_run.task_input_service import TaskInputService
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
    run = service.create_run(
        task.id, model_config_id=config_id, run_command=ConversationRunCommand(display_text="first")
    )
    return SimpleNamespace(
        task=task,
        config_id=config_id,
        run=run,
        inputs=TaskInputService(),
        service=service,
        context=get_conversation_task_context_service(),
        crud=ConversationRunCrud(),
    )


def _session():
    """返回使用替身执行器的调度入口：只断言启动参数，不真正驱动 workflow。"""
    from unittest.mock import MagicMock

    from app.service.conversation_run.workflow_session import WorkflowSession

    session = WorkflowSession()
    session._start = MagicMock()
    return session, SimpleNamespace(start=session._start)


def _starts(executor) -> list[int]:
    """替身执行器收到的启动（run_id）；入口节点由 workflow 按 Run 进度自行推导。"""
    return [call.args[0].id for call in executor.start.call_args_list]


def _real_context(store, *, run_state=None):
    """真实 Run 状态服务、inbox、Task 运行空间与 projector 组成的运行上下文；工具系统为空注册表。

    ``run_state`` 非空时替换节点直接取用的 Run 状态单例（经 ``SINGLETONS``，用例结束后复原）。
    """
    from app.config import configuration
    from app.core.tools.tool_registry import ToolRegistry
    from app.core.workflow.execution_state.runtime_context import RuntimeContext
    from app.service import depends as service_depends
    from app.service.depends import get_conversation_event_projector
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
    return RuntimeContext(
        run=store.crud.get(store.run.id),
        task=store.task,
        workspace=WorkspaceCrud().get(store.task.workspace_id),
        agent=SimpleNamespace(
            definition=SimpleNamespace(max_steps=5, structured_output=None), model_settings=None
        ),
        allows_tools=frozenset(),
        task_space=task_runtime_spaces.get_or_create(store.task.id),
        event_sink=get_conversation_event_projector().process,
    )


def _drop_pending(store):
    """清掉夹具建 Run 时入队的首条输入，供用例自行构造带附件的首条消息。"""
    from app.storage.crud.task_input_crud import TaskInputCrud

    with main_session_factory().begin() as session:
        TaskInputCrud().delete(
            session, [int(p.message_id) for p in store.inputs.pending(store.task.id)]
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


def test_consume_inbox_tracks_memory_sequence_and_projects_each_input(store, monkeypatch):
    """consume_inbox 一次转移整批 immediate 输入：内存列表与序号同步推进，每条经 sink 投影一次。"""
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder
    from app.core.events import UserInputConsumedEvent

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    second = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    events: list[object] = []
    context = _context_manager(store)
    context.begin_run(store.run, events.append)

    entries = context.consume_inbox()

    assert [e.message["content"] for e in entries] == ["first", "second"]
    assert [m.get("content") for m in context.load_message()] == ["system", "first", "second"]
    assert all(isinstance(event, UserInputConsumedEvent) for event in events)
    assert [(e.message_id, e.presentation.display_text) for e in events] == [
        (entries[0].message_id, "first"),
        (second, "second"),
    ]
    assert context._message_sequence == entries[-1].sequence + 1
    # 已消费的输入不会被下一次模型边界重复领取。
    assert context.consume_inbox() == []
    assert len(events) == 2


def test_edit_needs_no_unload_because_begin_run_rebuilds_memory(store, monkeypatch):
    """编辑重跑不卸载 manager：同一个对象在下一次 begin_run 时从库重建，只看到编辑后的上下文。"""
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run, lambda _event: None)
    [first] = context.consume_inbox()
    context.add_message({"role": "assistant", "content": "old answer"})
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))

    store.service.edit_latest(
        store.task.id,
        store.run.id,
        first.message_id,
        ConversationRunCommand(display_text="edited"),
        store.config_id,
        None,
    )
    context.begin_run(store.crud.get(store.run.id), lambda _event: None)

    assert [m.get("content") for m in context.load_message()] == ["system", "edited"]
    context.add_message({"role": "assistant", "content": "new answer"})
    sequences = [
        row.sequence
        for row in ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    ]
    assert len(sequences) == len(set(sequences))


def test_atomic_queue_consumption_and_multiple_users_same_run(store):
    second = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    pending = store.inputs.pending(store.task.id)
    assert [p.payload.message.input_text for p in pending] == ["first", "second"]
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    assert [e.message.get("content") for e in entries] == ["first", "second"]
    assert entries[-1].message_id == second
    assert all(e.run_id == store.run.id for e in entries)
    assert store.inputs.consume(store.task.id, store.run.id, 3) == []
    assert [e.message_id for e in store.context.entries_in_context(store.task.id)] == [
        p.message_id for p in pending
    ]


def test_consume_failure_rolls_back_inbox_and_context(store, monkeypatch):
    original = store.context.append

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("commit interrupted")

    monkeypatch.setattr(store.context, "append", fail)
    with pytest.raises(RuntimeError):
        store.inputs.consume(store.task.id, store.run.id, 1)
    assert len(store.inputs.pending(store.task.id)) == 1
    assert store.context.entries_in_context(store.task.id) == []


def test_consumed_edit_preserves_prefix_and_run_identity(store):
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "first response"}, 2
    )
    mid = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    store.inputs.consume(store.task.id, store.run.id, 3)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "discard"}, 4
    )
    store.crud.update_status_if_in(
        store.run.id,
        "cancelled",
        ("running",),
        execution_data=ConversationRunProgress(step_count=5, tool_error_count=2),
    )
    store.service.edit_latest(
        store.task.id,
        store.run.id,
        mid,
        ConversationRunCommand(display_text="edited"),
        store.config_id,
        None,
    )
    rows = store.context.entries_in_context(store.task.id)
    assert [e.message.get("content") for e in rows] == ["first", "first response", "edited"]
    assert rows[0].message_id == entries[0].message_id
    assert rows[-1].message_id == mid
    assert store.crud.get(store.run.id).status == "running"
    assert store.crud.get(store.run.id).execution_data == ConversationRunProgress(step_count=5)


def test_edit_keeps_replaced_history_out_of_context_and_transport(store):
    """编辑不删旧 context：被替换的行留给观测页，但不进模型上下文、不进快照，也不会被再次编辑命中。"""
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

    store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "first response"}, 2
    )
    mid = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    store.inputs.consume(store.task.id, store.run.id, 3)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "replaced answer"}, 4
    )
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))

    def edit(text: str) -> None:
        store.service.edit_latest(
            store.task.id,
            store.run.id,
            mid,
            ConversationRunCommand(display_text=text),
            store.config_id,
            None,
        )
        store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))

    edit("edited once")
    edit("edited twice")

    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    by_content = {row.message.get("content"): row for row in rows}
    for replaced in ("second", "replaced answer", "edited once"):
        row = by_content[replaced]
        assert (row.include_in_context, row.project_to_transport) == (False, False)
    latest = by_content["edited twice"]
    assert (latest.include_in_context, latest.project_to_transport) == (True, True)
    assert latest.message_id == mid
    # 被移出的行保留原序号，新消息只追加在最大序号之后。
    assert latest.sequence == max(row.sequence for row in rows)
    assert len({row.sequence for row in rows}) == len(rows)

    in_context = store.context.entries_in_context(store.task.id)
    assert [e.message.get("content") for e in in_context] == [
        "first",
        "first response",
        "edited twice",
    ]
    snapshot_texts = [
        part.get("text")
        for message in ConversationTaskStateService().rebuild_state(store.task.id)["runs"][0][
            "messages"
        ]
        for part in message["parts"]
        if part.get("type") == "text"
    ]
    assert "replaced answer" not in snapshot_texts
    assert "second" not in snapshot_texts
    assert "edited once" not in snapshot_texts
    assert "edited twice" in snapshot_texts


def test_restart_cancels_wait_and_preserves_inbox(store):
    store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(
        store.task.id,
        store.run.id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "tool", "arguments": json.dumps({})},
                }
            ],
        },
        2,
    )
    store.context.append(
        store.task.id,
        store.run.id,
        {"role": "tool", "content": "waiting", "tool_call_id": "c"},
        3,
        metadata=ToolMessageMetadata.model_validate(
            {
                "status": "running",
                "display_data": {"user_input_request": {"id": "old"}},
            }
        ),
    )
    store.crud.update_status_if_in(
        store.run.id,
        "waiting_for_input",
        ("running",),
        execution_data=ConversationRunProgress(
            step_count=2,
            continuation=ToolContinuation(
                observations=[],
                pending_call={
                    "id": "c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ),
        ),
    )
    store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="later"), store.config_id
    )
    recovered = store.service.recover_orphaned_runs()
    assert recovered == []
    assert store.crud.get(store.run.id).status == "waiting_for_input"
    assert store.crud.get(store.run.id).execution_data.continuation is not None
    assert store.inputs.pending(store.task.id)[0].payload.message.input_text == "later"
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].metadata.status == "running"
    assert store.service.recover_orphaned_runs() == []


def test_snapshot_groups_multiple_users_and_pending_inputs(store):
    store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(store.task.id, store.run.id, {"role": "assistant", "content": "reply"}, 2)
    second = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    store.inputs.consume(store.task.id, store.run.id, 3)
    store.context.append(store.task.id, store.run.id, {"role": "assistant", "content": "reply2"}, 4)
    queued = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="third"), store.config_id
    )
    state = ConversationTaskStateService().rebuild_state(store.task.id)
    assert [m["role"] for m in state["runs"][0]["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert state["runs"][0]["messages"][2]["id"] == second
    assert state["queued_messages"][0]["id"] == queued


def test_completed_run_handoff_claims_next_batch_without_duplicating_input(store):
    store.inputs.consume(store.task.id, store.run.id, 1)
    message_id = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="next"), store.config_id
    )
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    run = asyncio.run(_session()[0]._open_run(store.task.id))
    assert run.id != store.run.id
    assert run.status == "running"
    assert [p.message_id for p in store.inputs.pending(store.task.id)] == [message_id]
    entries = store.inputs.consume(store.task.id, run.id, 2)
    assert entries[0].run_id == run.id
    assert entries[0].message_id == message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_send_after_cancel_or_failure_opens_new_run_with_leftover_queue(store, status):
    """取消或失败后发消息不排队：直接新开 Run，残留的「排队」消息按序一并并入。"""
    store.inputs.consume(store.task.id, store.run.id, 1)
    leftover = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="later"), store.config_id, None, "after_run"
    )
    store.crud.update_status_if_in(store.run.id, status, ("running",))
    session, executor = _session()
    receipt = await session.submit(
        store.task.id,
        ConversationRunCommand(display_text="now"),
        model_config_id=store.config_id,
        reasoning_effort=None,
        delivery="after_run",
    )
    assert not receipt.queued
    [run_id] = _starts(executor)
    assert run_id != store.run.id
    assert store.crud.get(store.run.id).status == status
    entries = store.inputs.consume(store.task.id, run_id, 2)
    assert [e.message_id for e in entries][0] == leftover
    assert [e.message["content"] for e in entries] == ["later", "now"]


@pytest.mark.asyncio
async def test_send_while_running_queues_by_delivery(store):
    """运行中发送只进 inbox：模型边界只并入「立即」投递，「排队」投递留给下一个 Run。"""
    session, executor = _session()
    for text, delivery in (("now", "immediate"), ("later", "after_run")):
        receipt = await session.submit(
            store.task.id,
            ConversationRunCommand(display_text=text),
            model_config_id=store.config_id,
            reasoning_effort=None,
            delivery=delivery,
        )
        assert receipt.queued
    executor.start.assert_not_called()
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    assert [e.message["content"] for e in entries] == ["first", "now"]
    assert [p.delivery for p in store.inputs.pending(store.task.id)] == ["after_run"]


def test_release_all_immediate_only_updates_delivery_column(store):
    """放行只把 delivery 列批量改为 immediate，payload 原样不动；随后消费按序领取全部。"""
    from app.storage.crud.task_input_crud import TaskInputCrud
    from app.storage.model.task_input_model import TaskInputModel

    later = store.inputs.enqueue(
        store.task.id,
        ConversationRunCommand(display_text="later"),
        store.config_id,
        None,
        "after_run",
    )
    with main_session_factory()() as session:
        rows = TaskInputCrud().list_pending(session, store.task.id)
        before = {row.id: row.payload for row in rows}

    with main_session_factory().begin() as session:
        TaskInputCrud().release_all_immediate(session, store.task.id)

    with main_session_factory()() as session:
        rows = session.query(TaskInputModel).filter_by(task_id=store.task.id).all()
        assert {row.delivery for row in rows} == {"immediate"}
        assert {row.id: row.payload for row in rows} == before
    assert [p.delivery for p in store.inputs.pending(store.task.id)] == ["immediate", "immediate"]
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    assert [e.message["content"] for e in entries] == ["first", "later"]
    assert entries[-1].message_id == later


def test_task_input_delivery_rejects_unknown_value(store):
    """delivery 列只接受两种投递方式，持久化边界拒绝其它值。"""
    from sqlalchemy.exc import IntegrityError

    from app.storage.model.task_input_model import TaskInputModel

    with pytest.raises(IntegrityError):
        with main_session_factory().begin() as session:
            session.add(TaskInputModel(task_id=store.task.id, payload={}, delivery="later"))


@pytest.mark.asyncio
async def test_immediate_send_while_running_cannot_switch_model(store):
    from app.service.conversation_run.workflow_session import SessionConflictError

    with pytest.raises(SessionConflictError):
        await _session()[0].submit(
            store.task.id,
            ConversationRunCommand(display_text="now"),
            model_config_id=store.config_id + 1,
            reasoning_effort=None,
            delivery="immediate",
        )


@pytest.mark.asyncio
async def test_executor_handoff_waits_for_cleanup_and_starts_new_run(store, monkeypatch):
    """收尾期间入队的输入在旧执行释放后才交给新 Run，两个执行不重叠。"""
    import asyncio

    from app.service.conversation_run import workflow_session as session_module
    from app.service.conversation_run.workflow_session import WorkflowSession

    order = []
    finished = asyncio.Event()
    states = get_conversation_run_state_service()
    session = WorkflowSession()

    async def drive(run_id, definition=None):
        order.append(("start", run_id, session.task_execution(store.task.id)))
        sequence = store.context.max_sequence(store.task.id) + 1
        store.inputs.consume(store.task.id, run_id, sequence)
        if run_id == store.run.id:
            store.inputs.enqueue(
                store.task.id,
                ConversationRunCommand(display_text="during final"),
                store.config_id,
            )
        states.transition(run_id, RunTransition.COMPLETE, final_output="done")
        if run_id != store.run.id:
            finished.set()

    monkeypatch.setattr(session_module, "drive_run", drive)
    session._start(store.crud.get(store.run.id))
    await asyncio.wait_for(finished.wait(), 2)
    for execution in list(session._executions.values()):
        await execution.thread_task
    assert order[0] == ("start", store.run.id, store.run.id)
    # 新 Run 启动时执行器里只有它自己：旧执行已在闸门内释放。
    assert order[1][0] == "start" and order[1][1] != store.run.id and order[1][2] == order[1][1]
    assert store.inputs.pending(store.task.id) == []
    assert not session._executions


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
        store.inputs.enqueue(
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
    assert [p.payload.message.input_text for p in store.inputs.pending(store.task.id)] == [
        "during request"
    ]
    messages = ConversationTaskStateService().rebuild_state(store.task.id)["runs"][0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_queue_ack_does_not_start_another_executor(store):
    from app.assistant_transport.assistant_api import send_message
    from app.assistant_transport.request import SendMessageRequest

    session, executor = _session()
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
    session, executor = _session()
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
            reloaded.begin_run(store.run, get_conversation_event_projector().process)
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
    assert len(store.inputs.pending(store.task.id)) == 1
    executor.start.assert_not_called()


@pytest.mark.asyncio
async def test_wait_decision_is_validated_before_transition_and_committed_with_run(store):
    from app.core.tools.schemas import UserDecision, UserDecisionKind
    from app.service.conversation_run.workflow_session import SessionConflictError

    store.inputs.consume(store.task.id, store.run.id, 1)
    request = {
        "kind": "approval",
        "request_id": "current",
        "prompt": "continue?",
        "decisions": ["approve", "reject"],
        "draft_schema": None,
        "draft": None,
    }
    progress = ConversationRunProgress(
        step_count=2,
        continuation=ToolContinuation(
            observations=[{"user_input_request": request}],
            pending_call={
                "id": "c",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            },
        ),
    )
    store.crud.update_status_if_in(
        store.run.id, "waiting_for_input", ("running",), execution_data=progress
    )
    assert store.service.recover_orphaned_runs() == []
    assert store.crud.get(store.run.id).execution_data.continuation is not None
    session, executor = _session()
    with pytest.raises(SessionConflictError):
        await session.decide(
            store.task.id, UserDecision(kind=UserDecisionKind.APPROVE, request_id="stale")
        )
    assert store.crud.get(store.run.id).status == "waiting_for_input"
    decision = UserDecision(kind=UserDecisionKind.APPROVE, request_id="current")
    await session.decide(store.task.id, decision)
    assert _starts(executor) == [store.run.id]
    assert store.crud.get(store.run.id).execution_data.continuation.decision == decision
    assert store.crud.get(store.run.id).status == "running"
    with pytest.raises(SessionConflictError):
        await session.decide(store.task.id, decision)


@pytest.mark.asyncio
async def test_failed_resume_updates_existing_snapshot_without_consuming_queue(store):
    store.crud.update_status_if_in(store.run.id, "failed", ("running",))
    state = ConversationTaskStateService()
    assert state.rebuild_state(store.task.id)["runs"][0]["status"] == "failed"
    session, executor = _session()
    await session.resume(store.task.id)
    assert _starts(executor) == [store.run.id]
    assert state.get_state(store.task.id)["runs"][0]["status"] == "running"
    assert len(store.inputs.pending(store.task.id)) == 1


def test_sdk_context_roundtrip_keeps_identity_metadata_and_raw_arguments(store, monkeypatch):
    """数据库读回 SDK JSON；消息身份、用户展示和响应信息均不进入模型正文。"""
    import json

    from app.core.context.runtime_message_context_manager import (
        SystemPromptBuilder,
    )

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = _context_manager(store)
    context.begin_run(store.run, get_conversation_event_projector().process)
    context.consume_inbox()
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
    reloaded.begin_run(store.run, get_conversation_event_projector().process)
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
    context.begin_run(store.run, get_conversation_event_projector().process)
    context.consume_inbox()
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
    session, executor = _session()
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
    context.begin_run(store.run, get_conversation_event_projector().process)
    context.consume_inbox()
    events = sdk_events([{"content": "partial"}, {"content": " tail"}], finish_reason=None)
    last_event = list(events)[-1]
    context.save_model_response(
        last_event.snapshot, include_in_context=False, registered_tool_names=set()
    )
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].message["content"] == "partial tail"
    assert not rows[-1].include_in_context
    reloaded = _context_manager(store)
    reloaded.begin_run(store.run, get_conversation_event_projector().process)
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
    driver = asyncio.create_task(run_driver.drive_run(store.run.id))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        if not edit:
            store.inputs.enqueue(
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
            store.service.edit_latest(
                store.task.id,
                store.run.id,
                user.message_id,
                ConversationRunCommand(display_text="edited"),
                store.config_id,
                None,
            )
        else:
            assert [p.payload.message.input_text for p in store.inputs.pending(store.task.id)] == [
                "next"
            ]
            states.transition(store.run.id, RunTransition.RESUME)
        cancellation_registry.consume(store.task.id)
        await run_driver.drive_run(store.run.id)
        assert store.crud.get(store.run.id).status == "completed"
        assert all(m.get("content") != "partial" for m in requests[1])
        assert [m["content"] for m in requests[1] if m["role"] == "user"] == (
            ["edited"] if edit else ["first", "next"]
        )
        assert store.inputs.pending(store.task.id) == []
        assert model.close.await_count == 2
    finally:
        if not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        cancellation_registry.consume(store.task.id)


def test_queued_options_only_apply_when_handing_off_to_new_run(store):
    """同一 Run 消费追加消息不改配置；最终回答后的队列交接采用下一条的配置。"""
    store.inputs.enqueue(
        store.task.id,
        ConversationRunCommand(display_text="during", ban_tools=["write_file"]),
        store.config_id,
    )
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    assert [entry.message["content"] for entry in entries] == ["first", "during"]
    assert store.crud.get(store.run.id).options.ban_tools == []
    assert all("ban_tools" not in entry.metadata.presentation.model_dump() for entry in entries)
    next_id = store.inputs.enqueue(
        store.task.id,
        ConversationRunCommand(display_text="after final", ban_tools=["read_file"]),
        store.config_id,
    )
    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    run = asyncio.run(_session()[0]._open_run(store.task.id))
    assert run is not None
    assert run.id != store.run.id
    assert run.options.ban_tools == ["read_file"]
    assert store.inputs.pending(store.task.id)[0].message_id == next_id
    transferred = store.inputs.consume(store.task.id, run.id, 3)
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
    first_id = store.inputs.enqueue(
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
    store.inputs.consume(store.task.id, store.run.id, 1)
    second_id = store.inputs.enqueue(
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
    store.inputs.consume(store.task.id, store.run.id, 2)
    store.context.append(
        store.task.id, store.run.id, {"role": "assistant", "content": "discard"}, 3
    )
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    store.service.edit_latest(
        store.task.id,
        store.run.id,
        second_id,
        ConversationRunCommand(display_text="edited [[cosir-file:file]]", ban_tools=["write_file"]),
        store.config_id,
        None,
    )
    rows = store.context.entries_in_context(store.task.id)
    assert [row.message_id for row in rows] == [first_id, second_id]
    assert rows[0].message["content"] == f"first {first_path}"
    assert rows[1].message["content"] == f"edited {second_path}"
    assert rows[1].metadata.presentation.attachments[0]["path"] == str(second_path)
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
    message_id = store.inputs.enqueue(
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
    store.inputs.consume(store.task.id, store.run.id, 1)
    attachment.unlink()
    store.crud.update_status_if_in(store.run.id, "cancelled", ("running",))
    store.service.edit_latest(
        store.task.id,
        store.run.id,
        message_id,
        ConversationRunCommand(display_text="ignore that file"),
        store.config_id,
        None,
    )
    row = store.context.entries_in_context(store.task.id)[0]
    assert row.message == {"role": "user", "content": "ignore that file"}
    assert row.metadata.presentation.attachments == []


@pytest.mark.asyncio
async def test_concurrent_sends_to_idle_task_open_one_run_and_queue_the_other(store):
    """Task 闸门串行化并发发送：只新开一个 Run，另一条消息进入该 Run 的 inbox。"""
    import asyncio
    from unittest.mock import MagicMock

    from app.service.conversation_run.workflow_session import WorkflowSession

    store.crud.update_status_if_in(store.run.id, "completed", ("running",))
    started: list[int] = []
    session = WorkflowSession()
    session._start = MagicMock(side_effect=lambda run, *_args, **_kw: started.append(run.id))
    session.task_execution = lambda _task_id: started[-1] if started else None
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
    assert sorted(receipt.queued for receipt in receipts) == [False, True]
    assert len(started) == 1
    assert len(store.crud.list_by_task(store.task.id)) == 2
