"""真实 SQLite 验证 inbox 转移、编辑和重启边界。"""

import json
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.core.tools.schemas.tool_call_lifecycle_state import ToolCallLifecycleState
from app.models import ConversationRunCommand
from app.models.conversation_run_progress import (
    ConversationRunProgress,
    RunWaitingState,
    ToolBatchSnapshot,
)
from app.models.message_metadata import ToolMessageMetadata
from app.service.conversation_run.task_input_service import TaskInputService
from app.service.depends import (
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


def test_pending_edit_keeps_identity_without_starting_run(store):
    mid = store.inputs.pending(store.task.id)[0].message_id
    assert not store.inputs.edit_latest(
        store.task.id,
        store.run.id,
        mid,
        ConversationRunCommand(display_text="changed"),
        store.config_id,
        None,
    )
    assert store.inputs.pending(store.task.id)[0].message_id == mid
    assert store.inputs.pending(store.task.id)[0].payload.message.input_text == "changed"
    assert store.crud.get(store.run.id).status == "pending"


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
        ("pending",),
        execution_data=ConversationRunProgress(step_count=5, tool_error_count=2),
    )
    assert store.inputs.edit_latest(
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
        ("pending",),
        execution_data=ConversationRunProgress(
            step_count=2,
            waiting=RunWaitingState(
                instruction="",
                last_tool_results=ToolBatchSnapshot(instruction="", observations=[]),
                tool_call_lifecycle=ToolCallLifecycleState(),
            ),
        ),
    )
    store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="later"), store.config_id
    )
    recovered = store.service.recover_orphaned_runs()
    assert recovered[0].status == "cancelled"
    assert recovered[0].execution_data.waiting is None
    assert store.inputs.pending(store.task.id)[0].payload.message.input_text == "later"
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].metadata == ToolMessageMetadata(status="cancelled")
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
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )

    store.inputs.consume(store.task.id, store.run.id, 1)
    message_id = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="next"), store.config_id
    )
    store.crud.update_status_if_in(store.run.id, "completed", ("pending",))
    result = ConversationRunCommandService().start_queued_run(store.task.id)
    assert result.run.id != store.run.id
    assert result.run.status == "running"
    assert [p.message_id for p in store.inputs.pending(store.task.id)] == [message_id]
    entries = store.inputs.consume(store.task.id, result.run.id, 2)
    assert entries[0].run_id == result.run.id
    assert entries[0].message_id == message_id


@pytest.mark.parametrize("status", ["cancelled", "failed", "waiting_for_input"])
def test_paused_run_queue_does_not_auto_start(store, status):
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )

    store.crud.update_status_if_in(store.run.id, status, ("pending",))
    commands = ConversationRunCommandService()
    result = commands.submit_input(
        commands=[],
        model_config_id=store.config_id,
        reasoning_effort=None,
        task_id=store.task.id,
        run_command=ConversationRunCommand(display_text="queued"),
    )
    assert not result.should_start
    assert result.run.id == store.run.id
    assert commands.start_queued_run(store.task.id) is None
    assert store.crud.get(store.run.id).status == status


def test_wait_cancel_closes_request_without_starting_model(store, monkeypatch):
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )

    monkeypatch.setattr(
        ConversationTaskStateRebuilder, "get_tool_display", lambda _: {"verb": "执行"}
    )
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
                "display_data": {"user_input_request": {"request_id": "old"}},
            }
        ),
    )
    store.crud.update_status_if_in(
        store.run.id,
        "waiting_for_input",
        ("pending",),
        execution_data=ConversationRunProgress(step_count=1),
    )
    result = get_conversation_run_state_service().cancel_waiting_run(store.run.id)
    assert result.status == "cancelled"
    row = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)[-1]
    assert row.metadata.display_data is None
    assert row.metadata.status == "cancelled"


@pytest.mark.asyncio
async def test_executor_handoff_waits_for_cleanup_and_starts_new_run(store, monkeypatch):
    import asyncio

    from app.core.runtime import conversation_run_executor as executor_module
    from app.service import depends

    order = []
    finished = asyncio.Event()
    states = get_conversation_run_state_service()

    class Runtime:
        def resolve_agent_profile_for_run(self, run):
            return run

        async def run_agent(self, run, **kwargs):
            order.append(("start", run.id))
            sequence = store.context.max_sequence(store.task.id) + 1
            store.inputs.consume(store.task.id, run.id, sequence)
            if run.id == store.run.id:
                store.inputs.enqueue(
                    store.task.id,
                    ConversationRunCommand(display_text="during final"),
                    store.config_id,
                )
            states.complete_run_if_running(run.id, final_output="done")

        async def finalize_run(self, profile, run_id):
            await asyncio.sleep(0)
            order.append(("released", run_id))
            if run_id != store.run.id:
                finished.set()

    monkeypatch.setattr(executor_module, "get_runtime", lambda: Runtime())
    executor = executor_module.ConversationRunExecutor()
    monkeypatch.setattr(depends, "get_conversation_run_executor", lambda: executor)
    states.claim_pending_run(store.run.id)
    await executor.start(store.run.id, "fresh")
    await asyncio.wait_for(finished.wait(), 2)
    for execution in list(executor._executions.values()):
        await execution.thread_task
    assert order[0] == ("start", store.run.id)
    assert order[1] == ("released", store.run.id)
    assert order[2][0] == "start" and order[2][1] != store.run.id
    assert store.inputs.pending(store.task.id) == []
    assert not executor._executions
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_real_loop_consumes_between_requests_and_leaves_final_queue_for_next_run(
    store, monkeypatch
):
    from unittest.mock import AsyncMock

    import app.core.workflow.workflow as workflow_module
    from app.core.context.runtime_message_context_manager import SystemPromptBuilder
    from app.service.depends import get_conversation_event_projector

    states = get_conversation_run_state_service()
    states.claim_pending_run(store.run.id)
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
    monkeypatch.setattr(
        workflow_module,
        "get_tool_system",
        lambda: SimpleNamespace(user_input_decision_handlers=None),
    )
    ops = SimpleNamespace(
        agent_profile=SimpleNamespace(max_steps=5, structured_output=None),
        get_current_run=lambda: store.crud.get(store.run.id),
        get_current_task=lambda: store.task,
        get_current_workspace=lambda: WorkspaceCrud().get(store.task.workspace_id),
        task_tool_schemas=[],
        allows_tools=(),
        is_current_run_cancelled=lambda: False,
        save_execution=lambda data: store.crud.save_execution(store.run.id, data),
        process_event=get_conversation_event_projector().process,
        complete_run_if_running=lambda usage, **kw: states.complete_run_if_running(
            store.run.id, usage_stats=usage, **kw
        ),
    )
    await workflow_module.ReactLikeWorkflow().run(ops)
    assert requests == [["first"], ["first", "during request"]]
    responses = [
        row
        for row in store.context.entries_in_context(store.task.id)
        if row.message["role"] == "assistant"
    ]
    assert [row.message["content"] for row in responses] == ["partial", "done"]
    assert store.crud.get(store.run.id).status == "completed"
    assert [p.payload.message.input_text for p in store.inputs.pending(store.task.id)] == [
        "after final"
    ]
    messages = ConversationTaskStateService().rebuild_state(store.task.id)["runs"][0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_queue_ack_is_json_and_does_not_start_another_executor(store):
    import json
    from unittest.mock import AsyncMock

    from app.assistant_transport.assistant_api import assistant_transport
    from app.assistant_transport.request import AssistantTransportRequest
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )

    get_conversation_run_state_service().claim_pending_run(store.run.id)
    request = AssistantTransportRequest(
        taskId=store.task.id,
        threadId=f"task-{store.task.id}",
        modelConfigId=store.config_id,
        commands=[
            {
                "type": "add-message",
                "commandId": "queue-request",
                "message": {"role": "user", "parts": [{"type": "text", "text": "second"}]},
            }
        ],
    )
    executor = SimpleNamespace(start=AsyncMock())
    response = await assistant_transport(
        request=request, run_executor=executor, transport_service=TransportAssistantService()
    )
    body = json.loads(response.body)
    assert body["queued"] is True
    assert body["runId"] == store.run.id
    assert len(body["state"]["queued_messages"]) == 2
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-cosir-task-id"] == str(store.task.id)
    executor.start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_queue_commands_during_stream_preserve_output_and_replace_one_row(
    store, monkeypatch, buffered
):
    """真实 loop/SDK 聚合/SQLite/API：queue 不重建输出，待发 delta 不丢失、不重复。"""
    from unittest.mock import AsyncMock

    import app.core.workflow.workflow as workflow_module
    from app.assistant_transport.assistant_api import assistant_transport
    from app.assistant_transport.request import AssistantTransportRequest
    from app.assistant_transport.service.conversation_task_state_rebuilder import (
        ConversationTaskStateRebuilder,
    )
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )
    from app.config.constant import Constant
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager, SystemPromptBuilder
    from app.service.depends import get_conversation_event_projector

    states = get_conversation_run_state_service()
    states.claim_pending_run(store.run.id)
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MIN_CHARS", 8 if buffered else 0)
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS", 3600)
    state_service = ConversationTaskStateService()
    executor = SimpleNamespace(start=AsyncMock())
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
            reloaded = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
            assert [m["role"] for m in reloaded.load_message()] == ["system", "user"]
            before = state_service.get_state(store.task.id)
            assert [p["text"] for p in parts(before)] == [
                "thinking",
                first if buffered else first + "B",
            ]
            for edit in (False, True):
                request = AssistantTransportRequest(
                    taskId=store.task.id,
                    threadId=f"task-{store.task.id}",
                    modelConfigId=store.config_id,
                    runId=store.run.id if edit else None,
                    commands=[
                        {
                            "type": "add-message",
                            "commandId": "edit-queued" if edit else "queue",
                            "sourceId": store.inputs.pending(store.task.id)[-1].message_id
                            if edit
                            else None,
                            "message": {
                                "role": "user",
                                "parts": [{"type": "text", "text": "edited" if edit else "second"}],
                            },
                        }
                    ],
                )
                response = await assistant_transport(
                    request=request,
                    run_executor=executor,
                    transport_service=TransportAssistantService(),
                )
                body = json.loads(response.body)
                assert body["queued"]
                assert body["state"]["runs"] == before["runs"]
                assert body["state"]["queued_messages"][0]["parts"][0]["text"] == (
                    "edited" if edit else "second"
                )
                assert state_service.get_state(store.task.id)["runs"] == before["runs"]

    model = SimpleNamespace(stream=stream, close=AsyncMock())
    monkeypatch.setattr(
        workflow_module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=True),
    )
    monkeypatch.setattr(
        workflow_module,
        "get_tool_system",
        lambda: SimpleNamespace(user_input_decision_handlers=None),
    )
    operations = SimpleNamespace(
        agent_profile=SimpleNamespace(max_steps=5, structured_output=None),
        get_current_run=lambda: store.crud.get(store.run.id),
        get_current_task=lambda: store.task,
        get_current_workspace=lambda: WorkspaceCrud().get(store.task.workspace_id),
        task_tool_schemas=[],
        allows_tools=(),
        is_current_run_cancelled=lambda: False,
        save_execution=lambda data: store.crud.save_execution(store.run.id, data),
        process_event=get_conversation_event_projector().process,
        complete_run_if_running=lambda usage, **kw: states.complete_run_if_running(
            store.run.id, usage_stats=usage, **kw
        ),
    )
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


def test_wait_decision_is_validated_before_transition_and_committed_with_run(store):
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )
    from app.core.tools.schemas import UserDecision, UserDecisionKind

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
        waiting=RunWaitingState(
            instruction="",
            last_tool_results=ToolBatchSnapshot(
                instruction="", observations=[{"user_input_request": request}]
            ),
            tool_call_lifecycle=ToolCallLifecycleState(),
        ),
    )
    store.crud.update_status_if_in(
        store.run.id, "waiting_for_input", ("pending",), execution_data=progress
    )
    commands = ConversationRunCommandService()
    with pytest.raises(ValueError, match="有效决定"):
        commands.resume_run(
            store.task.id,
            store.run.id,
            user_decision=UserDecision(kind=UserDecisionKind.APPROVE, request_id="stale"),
        )
    assert store.crud.get(store.run.id).status == "waiting_for_input"
    decision = UserDecision(kind=UserDecisionKind.APPROVE, request_id="current")
    accepted = commands.resume_run(store.task.id, store.run.id, user_decision=decision)
    assert accepted.execution_mode == "resume_with_input"
    assert store.crud.get(store.run.id).execution_data.decision == decision
    assert accepted.initial_state["runs"][0]["status"] == "running"
    with pytest.raises(ValueError):
        commands.resume_run(store.task.id, store.run.id, user_decision=decision)


def test_failed_resume_updates_existing_snapshot_without_consuming_queue(store):
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )

    store.crud.update_status_if_in(store.run.id, "failed", ("pending",))
    state = ConversationTaskStateService()
    assert state.rebuild_state(store.task.id)["runs"][0]["status"] == "failed"
    accepted = ConversationRunCommandService().resume_run(store.task.id, store.run.id)
    assert accepted.initial_state["runs"][0]["status"] == "running"
    assert len(store.inputs.pending(store.task.id)) == 1


def test_sdk_context_roundtrip_keeps_identity_metadata_and_raw_arguments(store, monkeypatch):
    """数据库读回 SDK JSON；消息身份、用户展示和响应信息均不进入模型正文。"""
    import json

    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager, SystemPromptBuilder

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
    context.begin_run(store.run)
    store.inputs.consume_into_context(context, store.run.id)
    user = store.context.entries_in_context(store.task.id)[1]
    assert user.metadata.presentation.display_text == "first"
    assert "input_text" not in user.metadata.presentation.model_dump()
    assert user.message == {"role": "user", "content": "first"}
    assert user.message_id
    response_entry = None
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
            _event.snapshot, include_in_context=False, existing=response_entry
        )
        rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
        responses = [row for row in rows if row.message["role"] == "assistant"]
        assert len(responses) == 1
        assert responses[0].message_id == response_entry.message_id
        assert not responses[0].include_in_context
    context.save_model_response(_event.snapshot, include_in_context=True, existing=response_entry)
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
    reloaded = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
    assert reloaded.load_message() == context.load_message()
    assert [m["role"] for m in reloaded.load_message()] == ["system", "user", "assistant", "tool"]
    assert all(
        "user_input" not in m and "message_id" not in m and "usage" not in m
        for m in reloaded.load_message()
    )


@pytest.mark.asyncio
async def test_queue_during_tool_preview_keeps_pending_part_executable(store, monkeypatch):
    """已落库的半截工具调用不能因 queue 冷重建而把实时 pending 预览误标 cancelled。"""
    from unittest.mock import AsyncMock

    from app.assistant_transport.assistant_api import assistant_transport
    from app.assistant_transport.event import ToolCallCreatedEvent, ToolCallStatusChangedEvent
    from app.assistant_transport.request import AssistantTransportRequest
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager, SystemPromptBuilder
    from tests.sdk_fixtures import sdk_completion

    get_conversation_run_state_service().claim_pending_run(store.run.id)
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
    context.begin_run(store.run)
    store.inputs.consume_into_context(context, store.run.id)
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
    executor = SimpleNamespace(start=AsyncMock())
    response = await assistant_transport(
        request=AssistantTransportRequest(
            taskId=store.task.id,
            threadId=f"task-{store.task.id}",
            modelConfigId=store.config_id,
            commands=[
                {
                    "type": "add-message",
                    "commandId": "queue-tool-preview",
                    "message": {"role": "user", "parts": [{"type": "text", "text": "second"}]},
                }
            ],
        ),
        run_executor=executor,
        transport_service=TransportAssistantService(),
    )
    assert json.loads(response.body)["state"]["runs"] == before["runs"]
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
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager, SystemPromptBuilder

    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    context = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
    context.begin_run(store.run)
    store.inputs.consume_into_context(context, store.run.id)
    events = sdk_events([{"content": "partial"}, {"content": " tail"}], finish_reason=None)
    last_event = list(events)[-1]
    context.save_model_response(last_event.snapshot, include_in_context=False)
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].message["content"] == "partial tail"
    assert not rows[-1].include_in_context
    reloaded = RuntimeMessageContextManager(store.task.id, SimpleNamespace(), store.context)
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
    from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
    from app.core.runtime.runner import AgentRuntime
    from app.service.depends import get_conversation_event_projector

    states = get_conversation_run_state_service()
    states.claim_pending_run(store.run.id)
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
    monkeypatch.setattr(
        workflow_module,
        "get_tool_system",
        lambda: SimpleNamespace(user_input_decision_handlers=None),
    )

    def cancel_run(**kwargs):
        assert closed.is_set()
        return states.cancel_run_if_running(store.run.id, **kwargs)

    ops = SimpleNamespace(
        agent_profile=SimpleNamespace(max_steps=5, structured_output=None),
        get_current_run=lambda: store.crud.get(store.run.id),
        get_current_task=lambda: store.task,
        get_current_workspace=lambda: WorkspaceCrud().get(store.task.workspace_id),
        task_tool_schemas=[],
        allows_tools=(),
        is_current_run_cancelled=lambda: cancellation_registry.is_cancelled(store.run.id),
        save_execution=lambda data: store.crud.save_execution(store.run.id, data),
        process_event=get_conversation_event_projector().process,
        cancel_run_if_running=cancel_run,
        complete_run_if_running=lambda usage, **kw: states.complete_run_if_running(
            store.run.id, usage_stats=usage, **kw
        ),
    )
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._task_service = SimpleNamespace(get_task=TaskCrud().get)
    runtime._workspace_service = SimpleNamespace(get_workspace=WorkspaceCrud().get)
    runtime._conversation_run_state_service = states
    monkeypatch.setattr(runtime, "_build_operations", lambda *_: ops)
    monkeypatch.setattr(HookInterceptor, "async_safe_fire", AsyncMock())
    original_fail = states.fail_run_if_running

    def fail_run(*args, **kwargs):
        assert closed.is_set()
        state = ConversationTaskStateService().get_state(store.task.id)
        parts = state["runs"][0]["messages"][-1]["parts"]
        assert parts and all(part["status"] == "completed" for part in parts)
        return original_fail(*args, **kwargs)

    monkeypatch.setattr(states, "fail_run_if_running", fail_run)
    agent = SimpleNamespace(run=store.run)
    cancellation_registry.clear(store.run.id)
    driver = asyncio.create_task(runtime.run_agent(agent))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        if not edit:
            store.inputs.enqueue(
                store.task.id, ConversationRunCommand(display_text="next"), store.config_id
            )
        cancellation_registry.mark_cancelled(store.run.id)
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
            assert store.inputs.edit_latest(
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
            states.resume_cancelled_run(store.run.id)
        cancellation_registry.clear(store.run.id)
        await runtime.run_agent(agent, execution_mode="resume")
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
        cancellation_registry.clear(store.run.id)


def test_queued_options_only_apply_when_handing_off_to_new_run(store):
    """同一 Run 消费追加消息不改配置；最终回答后的队列交接采用下一条的配置。"""
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )

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
    store.crud.update_status_if_in(store.run.id, "completed", ("pending",))
    accepted = ConversationRunCommandService().start_queued_run(store.task.id)
    assert accepted is not None
    assert accepted.run.id != store.run.id
    assert accepted.run.options.ban_tools == ["read_file"]
    assert store.inputs.pending(store.task.id)[0].message_id == next_id
    transferred = store.inputs.consume(store.task.id, accepted.run.id, 3)
    assert len(transferred) == 1
    assert transferred[0].run_id == accepted.run.id
    assert transferred[0].message_id == next_id


def test_consumed_edit_uses_its_own_attachment_and_keeps_run_options(store, tmp_path):
    """多输入 Run 中编辑第二条消息，只复用该消息附件，不从第一条或 Run 取值。"""
    from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput

    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first")
    second_path.write_text("second")
    first_id = store.inputs.pending(store.task.id)[0].message_id
    store.inputs.edit_latest(
        store.task.id,
        store.run.id,
        first_id,
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
        None,
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
    store.crud.update_status_if_in(store.run.id, "cancelled", ("pending",))
    assert store.inputs.edit_latest(
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
    message_id = store.inputs.pending(store.task.id)[0].message_id
    store.inputs.edit_latest(
        store.task.id,
        store.run.id,
        message_id,
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
        None,
    )
    store.inputs.consume(store.task.id, store.run.id, 1)
    attachment.unlink()
    store.crud.update_status_if_in(store.run.id, "cancelled", ("pending",))
    assert store.inputs.edit_latest(
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
