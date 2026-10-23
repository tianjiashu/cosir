"""角色 metadata 的存储边界与实时/冷重建一致性。"""

import pytest
from pydantic import ValidationError

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.core.events import ToolCallStatusChangedEvent
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata, parse_message_metadata


@pytest.mark.parametrize(
    ("message", "metadata"),
    [
        ({"role": "system", "content": "system"}, {}),
        ({"role": "developer", "content": "policy"}, {}),
        ({"role": "user", "content": "internal notice"}, {"kind": "notice"}),
        (
            {"role": "user", "content": "resolved text"},
            {"kind": "user_input", "presentation": {"display_text": "visible text"}},
        ),
        (
            {"role": "assistant", "content": "answer"},
            {
                "completion_id": "response-1",
                "model": "test",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        ),
        (
            {"role": "assistant", "content": "answer"},
            {"finish_reason": "provider_custom"},
        ),
        (
            {"role": "tool", "tool_call_id": "call-1", "content": "private model result"},
            {
                "status": "running",
                "display_data": {"user_input_request": {"request_id": "request-1"}},
            },
        ),
    ],
)
def test_role_metadata_round_trip_without_polluting_sdk_message(message, metadata):
    record = ConversationTaskContextRecord(
        task_id=1,
        run_id=1,
        sequence=1,
        include_in_context=True,
        message=message,
        metadata=parse_message_metadata(message["role"], metadata),
    )
    restored = ConversationTaskContextRecord._from_model(record._to_model())
    assert restored.message == message
    assert restored.message_id == record.message_id
    assert restored.metadata == record.metadata


@pytest.mark.parametrize(
    ("role", "metadata"),
    [
        ("tool", {}),
        ("user", {"status": "completed"}),
        ("assistant", {"kind": "user_input", "presentation": {"display_text": "wrong role"}}),
        ("system", {"usage": {}}),
        ("user", {"user_input": {"display_text": "old wrapper"}}),
        ("tool", {"status": "completed", "status_hint": "obsolete"}),
    ],
)
def test_storage_boundary_rejects_role_mismatch_and_obsolete_shapes(role, metadata):
    with pytest.raises(ValidationError):
        parse_message_metadata(role, metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        ToolMessageMetadata(
            status="completed",
            display_data={
                "kind": "delegation-result",
                "child_task_id": 2,
                "role": "saved role",
            },
        ),
        ToolMessageMetadata(
            status="failed", error="执行失败", display_data={"status_hint": "失败"}
        ),
        ToolMessageMetadata(status="cancelled", error="已取消"),
        ToolMessageMetadata(
            status="running",
            display_data={"user_input_request": {"request_id": "wait-1"}},
            team_run_id=5,
        ),
    ],
)
def test_live_and_cold_tool_results_share_persisted_fields(metadata, monkeypatch):
    monkeypatch.setattr(
        ConversationTaskStateRebuilder, "get_tool_display", staticmethod(lambda _: {})
    )
    call = ConversationTaskContextRecord(
        task_id=1,
        run_id=1,
        sequence=1,
        include_in_context=True,
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "{}"},
                }
            ],
        },
    )
    result = ConversationTaskContextRecord(
        task_id=1,
        run_id=1,
        sequence=2,
        include_in_context=True,
        message={"role": "tool", "tool_call_id": "call-1", "content": "model-only result"},
        metadata=metadata,
    )
    initial = ConversationTaskStateRebuilder.build_pair_tool_part([call])["call-1"]
    initial["status"] = "running"
    initial["display_data"] = {"user_input_request": {"request_id": "old-request"}}
    initial["error"] = "old error"
    state = {"runs": [{"runId": 1, "messages": [{"role": "assistant", "parts": [initial]}]}]}
    event = ToolCallStatusChangedEvent(
        task_id=1,
        run_id=1,
        tool_call_id="call-1",
        status=metadata.status,
        error=metadata.error,
        display_data=metadata.display_data,
        team_run_id=metadata.team_run_id,
    )
    for mutation in plan_event(event, state):
        initial[mutation.path[-1]] = mutation.value
    restored = ConversationTaskContextRecord._from_model(result._to_model())
    cold = ConversationTaskStateRebuilder.build_pair_tool_part([call, restored])["call-1"]
    assert initial == cold
    assert "model-only result" not in str(cold)
    if metadata.display_data is not None:
        initial["display_data"]["mutated"] = True
        assert "mutated" not in metadata.display_data
        assert "mutated" not in cold["display_data"]


def test_final_tool_metadata_keeps_accumulated_terminal_preview():
    """终态摘要可被截断，实时终端预览仍保留累计全文。"""
    part = {
        "type": "tool-call",
        "toolCallId": "call-1",
        "status": "running",
        "display_data": {"kind": "terminal-result", "output": "full output"},
        "terminal_output_seq": 3,
    }
    state = {"runs": [{"runId": 1, "messages": [{"role": "assistant", "parts": [part]}]}]}
    event = ToolCallStatusChangedEvent(
        task_id=1,
        run_id=1,
        tool_call_id="call-1",
        status="completed",
        display_data={"kind": "terminal-result", "output": "truncated"},
    )
    for mutation in plan_event(event, state):
        part[mutation.path[-1]] = mutation.value
    assert part["status"] == "completed"
    assert part["display_data"]["output"] == "full output"
    assert event.display_data["output"] == "truncated"
