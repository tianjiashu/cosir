"""child-task tool display and canonical parent delegation regressions."""

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, ToolMessage

# Load the tool package before the transport event package to match the production
# dependency bootstrap and avoid the legacy import cycle during isolated collection.
import app.core.tools  # noqa: F401

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.core.tools.display.child_agent_display import (
    build_child_agent_result_display_data,
    build_child_agent_wait_display_data,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord


def _run(*, run_id: int, task_id: int, status: str, final_output: str | None = None) -> ConversationRunRecord:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        input_text="child input",
        status=status,
        created_at=now,
        updated_at=now,
        checkpoint_thread_id=f"checkpoint-{run_id}",
        final_output=final_output,
        end_reason=None if status == "completed" else "child_failed",
        agent_id="reviewer",
    )


def test_child_agent_display_builders_emit_stable_kinds_and_safe_fields() -> None:
    result = build_child_agent_result_display_data(
        operation="status",
        child_task_id=2,
        child_run_id=3,
        status="completed",
        agent_id="reviewer",
        agent_name="Review",
        final_output="summary",
        end_reason=None,
    )
    wait = build_child_agent_wait_display_data(
        child_task_id=2,
        child_run_id=3,
        status="completed",
        final_output="summary",
        end_reason=None,
        timed_out=False,
    )

    assert result == {
        "kind": "child-agent-result",
        "operation": "status",
        "child_task_id": 2,
        "child_run_id": 3,
        "status": "completed",
        "agent_id": "reviewer",
        "agent_name": "Review",
        "final_output": "summary",
        "end_reason": None,
    }
    assert wait["kind"] == "child-agent-wait-result"
    assert wait["messages"][0]["end_reason"] is None


def test_cold_rebuild_uses_persisted_delegation_display_data_without_inference(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    rows = [
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate-call",
                        "name": "delegate_task",
                        "args": {
                            "child_agent_id": "reviewer",
                            "agent_name": "Review",
                            "message": "inspect",
                        },
                    }
                ],
            ),
            include_in_context=True,
            sequence=1,
            transport_metadata={},
        ),
        ConversationTaskContextRecord(
            task_id=1,
            run_id=10,
            message=ToolMessage(content="started", tool_call_id="delegate-call"),
            include_in_context=True,
            sequence=2,
            transport_metadata={
                "status": "completed",
                "display_data": {
                    "kind": "delegation-result",
                    "title": "Review",
                    "child_agent_id": "reviewer",
                    "child_task_id": 2,
                    "child_run_id": 20,
                    "status": "running",
                },
            },
        ),
    ]

    # Legacy delegation rows are deliberately ignored: display_data is the only dynamic
    # delegation payload accepted by cold rebuild.
    parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows, [object()])

    assert parts["delegate-call"]["display_data"] == rows[1].transport_metadata["display_data"]
    assert "final_output" not in parts["delegate-call"]["display_data"]
