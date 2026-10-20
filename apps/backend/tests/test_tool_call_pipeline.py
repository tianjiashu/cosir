"""工具批次的新边界：解析、执行、等待恢复和 canonical 结果投影。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.llm_provider.message_codec import assistant_message
from app.core.tools.schemas import ToolObservation, UserDecision, UserDecisionKind, UserInputRequest
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.node_helper import streaming_part_state_machine as streaming
from app.core.workflow.node_helper import tool_observation_projection as projection
from app.core.workflow.nodes import tools_node, wait_user_node
from app.core.workflow.tool_continuation import freeze_tool_continuation, restore_tool_batch
from app.models.conversation_run_progress import ToolContinuation
from tests.sdk_fixtures import sdk_completion


def _raw(call_id: str = "call-1", arguments: str = "{}") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": arguments},
    }


def _state(
    *, calls: list[dict] | None = None, observations: list[dict] | None = None
) -> ReactExecutionState:
    return ReactExecutionState(
        step_count=1,
        tool_error_count=0,
        next_node=ReactRoute.TOOLS,
    )


def test_assistant_message_filters_identity_and_registry_only() -> None:
    calls = [
        _raw("valid"),
        _raw(""),
        {**_raw("ghost"), "function": {"name": "ghost_tool", "arguments": "{}"}},
        _raw("valid", '{"path":'),
    ]
    message = sdk_completion(tool_calls=calls).choices[0].message
    projected = assistant_message(message, {"read_file"})
    assert [call["id"] for call in projected["tool_calls"]] == ["valid", "valid"]
    assert projected["tool_calls"][1]["function"]["arguments"] == '{"path":'


def test_streaming_preview_only_creates_allowed_registered_tool(monkeypatch) -> None:
    events = []
    operations = SimpleNamespace(
        vaild_tool_name=lambda name: name in {"read_file", "write_file"},
        allows_tools={"read_file"},
        all_vaild_tools=[SimpleNamespace(name="read_file", display=None)],
    )
    monkeypatch.setattr(
        streaming, "_runtime_context", lambda: SimpleNamespace(operations=operations)
    )
    monkeypatch.setattr(streaming, "get_stream_writer", lambda: events.append)
    parts = streaming.StreamingPartStateMachine(task_id=1, run_id=2, step_id="step-1")

    assert parts.create_tool_call("visible", "read_file") is True
    assert parts.create_tool_call("hidden", "write_file") is False
    assert [event.tool_call_id for event in events] == ["visible"]


def test_tools_node_decodes_arguments_and_preserves_approved_decision(monkeypatch) -> None:
    received = []
    events = []
    decision = UserDecision(request_id="review-1", kind=UserDecisionKind.APPROVE)

    async def run_calls(_task_id, calls, _step_id, _loop):
        received.extend(calls)
        return SimpleNamespace(
            observations=[
                ToolObservation(
                    tool_name="read_file", status="success", tool_call_id="call-1", content="ok"
                )
            ]
        )

    operations = SimpleNamespace(
        get_current_task=lambda: SimpleNamespace(id=3),
        get_current_run=lambda: SimpleNamespace(id=4, task_id=3),
        all_vaild_tools=[SimpleNamespace(name="read_file", need_HIL=False)],
        run_tool_calls=run_calls,
    )
    runtime = SimpleNamespace(
        operations=operations,
        batch=ToolBatch(
            calls=[_raw(arguments='{"path":"a.py"}')],
            valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")},
            approved_call=("call-1", decision),
        ),
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(tools_node, "get_stream_writer", lambda: events.append)
    patch = asyncio.run(tools_node._tools_node(_state()))

    assert len(received) == 1
    assert received[0].arguments == {"path": "a.py"}
    assert received[0].user_decision == decision
    assert runtime.batch.approved_call is None
    assert events[0].status == "running"
    assert patch["next_node"] is ReactRoute.OBSERVE

    runtime.batch = ToolBatch(calls=[_raw("next-call")])
    asyncio.run(tools_node._tools_node(_state()))
    assert received[-1].user_decision is None


def test_banned_registered_call_reaches_executor_without_tool_card(monkeypatch) -> None:
    received = []
    events = []

    async def run_calls(_task_id, calls, _step_id, _loop):
        received.extend(calls)
        return SimpleNamespace(observations=[])

    operations = SimpleNamespace(
        get_current_task=lambda: SimpleNamespace(id=3),
        get_current_run=lambda: SimpleNamespace(id=4, task_id=3),
        all_vaild_tools=[
            SimpleNamespace(name="read_file", need_HIL=False),
            SimpleNamespace(name="write_file", need_HIL=False),
        ],
        run_tool_calls=run_calls,
    )
    banned = {**_raw("banned"), "function": {"name": "write_file", "arguments": "{}"}}
    runtime = SimpleNamespace(
        operations=operations,
        batch=ToolBatch(
            calls=[_raw("visible"), banned],
            valid_tool_calls={
                "visible": ValidToolCall(name="read_file", projection="projected"),
                "banned": ValidToolCall(name="write_file", projection="banned"),
            },
        ),
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(tools_node, "get_stream_writer", lambda: events.append)

    asyncio.run(tools_node._tools_node(_state()))

    assert [call.call_id for call in received] == ["visible", "banned"]
    assert [event.tool_call_id for event in events] == ["visible"]


def test_unmarked_wait_request_cannot_suspend_a_multi_call_batch(monkeypatch) -> None:
    async def run_calls(_task_id, _calls, _step_id, _loop):
        return SimpleNamespace(
            observations=[
                ToolObservation(
                    tool_name="read_file",
                    status="success",
                    tool_call_id="call-1",
                    user_input_request=UserInputRequest(request_id="review", kind="approval"),
                )
            ]
        )

    runtime = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=3),
            get_current_run=lambda: SimpleNamespace(id=4, task_id=3),
            all_vaild_tools=[SimpleNamespace(name="read_file", need_HIL=False)],
            run_tool_calls=run_calls,
        ),
        batch=ToolBatch(calls=[_raw("call-1"), _raw("call-2")]),
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)

    with pytest.raises(ValueError, match="待决请求必须独占工具批次"):
        asyncio.run(tools_node._tools_node(_state()))


def test_malformed_json_never_reaches_tool_executor(monkeypatch) -> None:
    called = []
    operations = SimpleNamespace(
        get_current_task=lambda: SimpleNamespace(id=3),
        get_current_run=lambda: SimpleNamespace(id=4, task_id=3),
        all_vaild_tools=[SimpleNamespace(name="read_file", need_HIL=False)],
        run_tool_calls=lambda *args: called.append(args),
    )
    runtime = SimpleNamespace(
        operations=operations,
        batch=ToolBatch(
            calls=[_raw(arguments='{"path":')],
            valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")},
        ),
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)

    asyncio.run(tools_node._tools_node(_state()))

    assert called == []
    assert runtime.batch.observations[0]["status"] == "error"


def test_internal_artifact_does_not_enter_tool_batch_snapshot() -> None:
    observation = ToolObservation(
        tool_name="read_file",
        status="success",
        tool_call_id="call-1",
        artifact_data={"secret": "internal"},
    )
    assert "artifact_data" not in tools_node._summary(observation)


def test_waiting_continuation_restores_only_projected_target() -> None:
    batch = ToolBatch(
        calls=[_raw()],
        observations=[{"tool_call_id": "call-1", "status": "running"}],
        valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")},
    )
    frozen = freeze_tool_continuation(batch, _raw())
    persisted = ToolContinuation.model_validate_json(frozen.model_dump_json())
    restored = restore_tool_batch(persisted)
    assert [call["id"] for call in restored.calls] == ["call-1"]
    assert restored.observations == batch.observations
    assert restored.was_projected("call-1")


def test_waiting_card_is_persisted_before_live_event(monkeypatch) -> None:
    order = []
    runtime = SimpleNamespace(
        operations=SimpleNamespace(get_current_run=lambda: SimpleNamespace(id=4, task_id=3)),
        batch=ToolBatch(
            valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")}
        ),
    )
    monkeypatch.setattr(projection, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(
        projection,
        "_message_context",
        lambda: SimpleNamespace(add_message=lambda *_args, **_kwargs: order.append("persist")),
    )
    monkeypatch.setattr(
        projection, "get_stream_writer", lambda: lambda _event: order.append("event")
    )
    projection.project_waiting_request(
        {
            "tool_call_id": "call-1",
            "tool_name": "read_file",
            "display_data": {},
            "user_input_request": {
                "kind": "approval",
                "request_id": "review-1",
                "prompt": "continue?",
                "decisions": ["approve", "reject"],
            },
        },
        step_id="step-1",
    )
    assert order == ["persist", "event"]


def test_approval_replays_one_call_and_rejection_only_settles(monkeypatch) -> None:
    request = {
        "kind": "approval",
        "request_id": "review-1",
        "prompt": "continue?",
        "decisions": ["approve", "reject"],
    }
    observation = {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "content": "",
        "error": "",
        "reason": "",
        "retryable": False,
        "display_data": {},
        "user_input_request": request,
    }
    runtime = SimpleNamespace(
        operations=SimpleNamespace(
            allows_tools={"read_file"},
            get_current_run=lambda: SimpleNamespace(id=4),
        ),
        batch=ToolBatch(calls=[_raw(), _raw("call-2")], observations=[observation]),
        user_input_decision_handlers=SimpleNamespace(handle_rejection=lambda *_: None),
    )
    monkeypatch.setattr(wait_user_node, "_runtime_context", lambda: runtime)
    state = _state()

    approved = wait_user_node.wait_user_node(
        state, UserDecision(request_id="review-1", kind=UserDecisionKind.APPROVE)
    )
    approved_calls = [call["id"] for call in runtime.batch.calls]
    runtime.batch = ToolBatch(calls=[_raw(), _raw("call-2")], observations=[observation])
    rejected = wait_user_node.wait_user_node(
        state, UserDecision(request_id="review-1", kind=UserDecisionKind.REJECT)
    )

    assert approved["next_node"] is ReactRoute.TOOLS
    assert approved_calls == ["call-1"]
    assert rejected["next_node"] is ReactRoute.OBSERVE
    assert runtime.batch.observations[0]["status"] == "cancelled"


def test_observation_persists_before_event_and_hides_unprojected_calls(monkeypatch) -> None:
    order = []
    runtime = SimpleNamespace(
        run=SimpleNamespace(id=4, task_id=3),
        operations=SimpleNamespace(
            get_current_run=lambda: SimpleNamespace(id=4, task_id=3),
            to_tool_model_message=lambda obs: {
                "role": "tool",
                "tool_call_id": obs.tool_call_id,
                "content": obs.content,
            },
        ),
        batch=ToolBatch(
            valid_tool_calls={
                "visible": ValidToolCall(name="read_file", projection="projected"),
                "hidden": ValidToolCall(name="read_file", projection="banned"),
            }
        ),
    )
    written_metadata = []

    def add_message(_message, *, metadata: object):
        order.append("persist")
        written_metadata.append(metadata)
        return "appended"

    monkeypatch.setattr(projection, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(
        projection, "_message_context", lambda: SimpleNamespace(add_message=add_message)
    )
    monkeypatch.setattr(
        projection, "get_stream_writer", lambda: lambda event: order.append(event.tool_call_id)
    )

    def summary(call_id: str) -> dict:
        return {
            "tool_call_id": call_id,
            "tool_name": "read_file",
            "status": "success",
            "content": "ok",
            "error": "",
            "reason": "",
            "retryable": False,
            "display_data": {},
        }

    count, errors = projection.settle_observations(
        [summary("visible"), summary("hidden")], step_id="step-1", inherited_error_count=2
    )

    assert (count, errors) == (0, 0)
    assert order == ["persist", "visible", "persist"]
    assert [item.hidden for item in written_metadata] == [False, True]
