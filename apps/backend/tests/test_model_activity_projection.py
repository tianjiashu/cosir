"""Current model activity is distinct from conversation content and Run lifecycle."""

from datetime import UTC, datetime, timedelta

from app.assistant_transport.projection.model_activity_projection import plan_model_activity
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.events import AssistantTextDeltaEvent, ModelActivityEvent
from app.utils.datetime_utils import to_text

T0 = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)


def _apply(state, event):
    result = plan_model_activity(event, state)
    if result:
        state["activity"]["modelActivity"] = result[0].value
    return result


def test_phase_without_message_and_old_finish():
    state = empty_snapshot()
    state["activity"] = {"status": "running", "modelActivity": None}
    event = ModelActivityEvent(session_id=1, run_id=1, step_id="step-2", phase="waiting", occurred_at=T0)
    assert _apply(state, event)[0].value == {"stepId": "step-2", "phase": "waiting", "startedAt": to_text(T0)}
    assert plan_model_activity(event.model_copy(update={"step_id": "step-1", "phase": None}), state) == []
    assert plan_model_activity(event.model_copy(update={"phase": None}), state)[0].value is None
    assert not state["messages"]


# 目的：「思考中」计这一个模型步；潜在缺陷：换阶段把计时清零，或新的一步接着上一步的起点算。
def test_started_at_follows_model_step_not_phase():
    state = empty_snapshot()
    state["activity"] = {"status": "running", "modelActivity": None}
    first = ModelActivityEvent(session_id=1, run_id=1, step_id="step-1", phase="waiting", occurred_at=T0)
    _apply(state, first)
    later = first.model_copy(update={"phase": "reasoning", "occurred_at": T0 + timedelta(seconds=9)})
    assert _apply(state, later)[0].value["startedAt"] == to_text(T0)
    _apply(state, later.model_copy(update={"phase": None}))
    assert state["activity"]["modelActivity"] is None
    nxt = first.model_copy(update={"step_id": "step-2", "occurred_at": T0 + timedelta(seconds=30)})
    assert _apply(state, nxt)[0].value["startedAt"] == to_text(T0 + timedelta(seconds=30))


def test_terminal_run_ignores_activity_and_reasoning_never_becomes_content():
    state = empty_snapshot()
    state["activity"] = {"status": "cancelled", "modelActivity": None}
    assert (
        plan_model_activity(
            ModelActivityEvent(session_id=1, run_id=1, step_id="step-2", phase="reasoning"), state
        )
        == []
    )
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AssistantTextDeltaEvent(
            source_sequence=1, session_id=1, run_id=1, turn=1, part="reasoning", delta="private reasoning"
        )
