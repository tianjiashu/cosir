"""Current model activity is distinct from conversation content and Run lifecycle."""

from app.assistant_transport.projection.message_event_projection import plan_assistant_text_delta
from app.assistant_transport.projection.model_activity_projection import plan_model_activity
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.events import AssistantTextDeltaEvent, ModelActivityEvent


def test_phase_without_message_and_old_finish():
    state = empty_snapshot()
    state["activity"] = {"runId": 1, "status": "running", "modelActivity": None}
    event = ModelActivityEvent(task_id=1, run_id=1, step_id="step-2", phase="waiting")
    result = plan_model_activity(event, state)
    assert result[0].value == {"stepId": "step-2", "phase": "waiting"}
    state["activity"]["modelActivity"] = result[0].value
    assert (
        plan_model_activity(event.model_copy(update={"step_id": "step-1", "phase": None}), state)
        == []
    )
    assert plan_model_activity(event.model_copy(update={"phase": None}), state)[0].value is None
    assert not state["messages"]


def test_terminal_run_ignores_activity_and_reasoning_never_becomes_content():
    state = empty_snapshot()
    state["activity"] = {"runId": 1, "status": "cancelled", "modelActivity": None}
    assert (
        plan_model_activity(
            ModelActivityEvent(task_id=1, run_id=1, step_id="step-2", phase="reasoning"), state
        )
        == []
    )
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="reasoning", delta="private reasoning")
