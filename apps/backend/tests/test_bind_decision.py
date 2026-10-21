"""用户决定的唯一校验点：绑定到等待中 Run 的待决调用之前核对请求与种类。"""

import pytest

from app.core.workflow.node_helper.user_input_projection import DecisionMismatch, bind_decision
from app.models.conversation_run_progress import ConversationRunProgress, ToolContinuation
from app.models.user_decision import UserDecision, UserDecisionKind


def _waiting(decisions: list[str]) -> ConversationRunProgress:
    return ConversationRunProgress(
        step_count=2,
        continuation=ToolContinuation(
            observations=[
                {
                    "tool_call_id": "call-1",
                    "tool_name": "review",
                    "status": "success",
                    "content": "waiting",
                    "user_input_request": {
                        "request_id": "req-1",
                        "kind": "review",
                        "prompt": "allow?",
                        "decisions": decisions,
                    },
                }
            ],
            pending_call={
                "id": "call-1",
                "type": "function",
                "function": {"name": "review", "arguments": "{}"},
            },
        ),
    )


def test_matching_decision_is_bound_to_continuation() -> None:
    decision = UserDecision(request_id="req-1", kind=UserDecisionKind.APPROVE)

    bound = bind_decision(_waiting(["approve", "reject"]), decision)

    assert bound.continuation.decision == decision
    assert bound.step_count == 2


@pytest.mark.parametrize(
    "decision",
    [
        UserDecision(request_id="other", kind=UserDecisionKind.APPROVE),
        UserDecision(request_id="req-1", kind=UserDecisionKind.REJECT),
    ],
)
def test_mismatched_decision_is_rejected(decision: UserDecision) -> None:
    with pytest.raises(DecisionMismatch):
        bind_decision(_waiting(["approve"]), decision)


def test_force_reject_becomes_plain_reject_of_current_request() -> None:
    """系统合成的强制驳回不核对 request_id，也不要求请求声明驳回。"""

    bound = bind_decision(_waiting(["approve"]), UserDecision(kind=UserDecisionKind.FORCE_REJECT))

    assert bound.continuation.decision == UserDecision(
        request_id="req-1", kind=UserDecisionKind.REJECT
    )


def test_progress_without_pending_request_is_rejected() -> None:
    with pytest.raises(DecisionMismatch):
        bind_decision(ConversationRunProgress(), UserDecision(kind=UserDecisionKind.FORCE_REJECT))
