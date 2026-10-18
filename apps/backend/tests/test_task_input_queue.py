"""真实 SQLite 验证 inbox 转移、编辑和重启边界。"""

from types import SimpleNamespace

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.core.llm_provider.messages import AIMessage, HumanMessage, ToolMessage
from app.models import ConversationRunCommand
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
    assert [p["input_text"] for p in pending] == ["first", "second"]
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    assert [e.message.content for e in entries] == ["first", "second"]
    assert entries[-1].message.id == second
    assert all(e.run_id == store.run.id for e in entries)
    assert store.inputs.consume(store.task.id, store.run.id, 3) == []
    assert [e.message.id for e in store.context.entries_in_context(store.task.id)] == [
        p["id"] for p in pending
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
    mid = store.inputs.pending(store.task.id)[0]["id"]
    assert not store.inputs.edit_latest(
        store.task.id,
        store.run.id,
        mid,
        ConversationRunCommand(display_text="changed"),
        store.config_id,
        None,
    )
    assert store.inputs.pending(store.task.id)[0]["id"] == mid
    assert store.inputs.pending(store.task.id)[0]["input_text"] == "changed"
    assert store.crud.get(store.run.id).status == "pending"


def test_consumed_edit_preserves_prefix_and_run_identity(store):
    entries = store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(store.task.id, store.run.id, AIMessage(content="first response"), 2)
    mid = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    store.inputs.consume(store.task.id, store.run.id, 3)
    store.context.append(store.task.id, store.run.id, AIMessage(content="discard"), 4)
    store.crud.update_status_if_in(
        store.run.id,
        "cancelled",
        ("pending",),
        execution_data={"step_count": 5, "tool_error_count": 2},
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
    assert [e.message.content for e in rows] == ["first", "first response", "edited"]
    assert rows[0].message.id == entries[0].message.id
    assert rows[-1].message.id == mid
    assert store.crud.get(store.run.id).status == "running"
    assert store.crud.get(store.run.id).execution_data == {"step_count": 5, "tool_error_count": 0}


def test_restart_cancels_wait_and_preserves_inbox(store):
    store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(
        store.task.id,
        store.run.id,
        AIMessage(content="", tool_calls=[{"id": "c", "name": "tool", "args": {}}]),
        2,
    )
    store.context.append(
        store.task.id,
        store.run.id,
        ToolMessage(content="waiting", tool_call_id="c"),
        3,
        transport_metadata={
            "status": "running",
            "display_data": {"user_input_request": {"id": "old"}},
        },
    )
    store.crud.update_status_if_in(
        store.run.id,
        "waiting_for_input",
        ("pending",),
        execution_data={"step_count": 2, "waiting": {"old": True}},
    )
    store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="later"), store.config_id
    )
    recovered = store.service.recover_orphaned_runs()
    assert recovered[0].status == "cancelled"
    assert "waiting" not in recovered[0].execution_data
    assert store.inputs.pending(store.task.id)[0]["input_text"] == "later"
    rows = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)
    assert rows[-1].transport_metadata == {
        "status": "cancelled",
        "display_data": None,
        "error": None,
    }
    assert store.service.recover_orphaned_runs() == []


def test_snapshot_groups_multiple_users_and_pending_inputs(store):
    store.inputs.consume(store.task.id, store.run.id, 1)
    store.context.append(store.task.id, store.run.id, AIMessage(content="reply"), 2)
    second = store.inputs.enqueue(
        store.task.id, ConversationRunCommand(display_text="second"), store.config_id
    )
    store.inputs.consume(store.task.id, store.run.id, 3)
    store.context.append(store.task.id, store.run.id, AIMessage(content="reply2"), 4)
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
    assert [p["id"] for p in store.inputs.pending(store.task.id)] == [message_id]
    entries = store.inputs.consume(store.task.id, result.run.id, 2)
    assert entries[0].run_id == result.run.id
    assert entries[0].message.id == message_id


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
        AIMessage(tool_calls=[{"id": "c", "name": "tool", "args": {}}]),
        2,
    )
    store.context.append(
        store.task.id,
        store.run.id,
        ToolMessage(content="waiting", tool_call_id="c"),
        3,
        transport_metadata={
            "status": "running",
            "display_data": {"user_input_request": {"request_id": "old"}},
        },
    )
    store.crud.update_status_if_in(
        store.run.id, "waiting_for_input", ("pending",), execution_data={"step_count": 1}
    )
    result = get_conversation_run_state_service().cancel_waiting_run(store.run.id)
    assert result.status == "cancelled"
    row = ConversationTaskContextCrud().get(store.task.id, include_in_context=False)[-1]
    assert row.transport_metadata["display_data"] is None
    assert row.transport_metadata["status"] == "cancelled"


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

    from app.core.context.runtime_context_manager import SystemPromptBuilder
    from app.core.llm_provider.messages import AIMessageChunk
    from app.core.workflows.react import workflow as workflow_module
    from app.service.depends import get_conversation_event_projector

    states = get_conversation_run_state_service()
    states.claim_pending_run(store.run.id)
    monkeypatch.setattr(SystemPromptBuilder, "build", lambda *_: "system")
    requests = []

    async def stream(messages, **kwargs):
        requests.append([m.content for m in messages if isinstance(m, HumanMessage)])
        text = "during request" if len(requests) == 1 else "after final"
        store.inputs.enqueue(
            store.task.id, ConversationRunCommand(display_text=text), store.config_id
        )
        yield AIMessageChunk(
            content="partial" if len(requests) == 1 else "done",
            response_metadata={"finish_reason": "length" if len(requests) == 1 else "stop"},
        )

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
    assert store.crud.get(store.run.id).status == "completed"
    assert [p["input_text"] for p in store.inputs.pending(store.task.id)] == ["after final"]
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
    progress = {
        "step_count": 2,
        "tool_error_count": 0,
        "waiting": {"last_tool_results": {"observations": [{"user_input_request": request}]}},
    }
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
    assert store.crud.get(store.run.id).execution_data["decision"] == decision.model_dump(
        mode="json"
    )
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


def test_legacy_graph_schema_is_rejected_without_changing_user_data(tmp_path):
    from sqlalchemy import create_engine, inspect

    from app.storage.store_engines import initialize_app_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE conversation_runs (id INTEGER PRIMARY KEY, checkpoint_thread_id TEXT)"
        )
    with pytest.raises(RuntimeError, match="旧版执行数据"):
        initialize_app_schema(engine)
    assert inspect(engine).get_table_names() == ["conversation_runs"]
    engine.dispose()
