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
from app.models.task_record import TaskRecord


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


def _task(task_id: int, *, parent_run_id: int | None = None, title: str = "Review") -> TaskRecord:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    return TaskRecord(
        id=task_id,
        workspace_id=1,
        title=title,
        created_at=now,
        updated_at=now,
        task_type="delegate_task" if parent_run_id is not None else "user",
        parent_task_id=1 if parent_run_id is not None else None,
        parent_run_id=parent_run_id,
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


def test_cold_rebuild_uses_terminal_child_run_instead_of_stale_tool_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    parent_run = _run(run_id=10, task_id=1, status="completed")
    child = _task(2, parent_run_id=10)
    child_run = _run(run_id=20, task_id=2, status="completed", final_output="done")
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

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(
        rows,
        child_task_states=((child, child_run),),
    )

    assert parts["delegate-call"]["display_data"]["status"] == "completed"
    assert parts["delegate-call"]["display_data"]["final_output"] == "done"
    assert parent_run.id == 10
