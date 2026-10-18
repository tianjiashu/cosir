"""human-in-the-loop 纯投影函数的契约测试。"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.core.tools.schemas.tool_observation import ToolObservation
from app.core.tools.schemas.user_decision import UserDecision, UserDecisionKind
from app.core.tools.schemas.user_input_request import UserInputRequest
from app.core.workflow.node_helper.user_input_projection import (
    apply_decision_to_observations,
    extract_request,
    request_call_id,
)


def _observation(request: object = ...) -> dict[str, object]:
    if request is ...:
        request = {
            "kind": "agent_team_review",
            "request_id": "12",
            "prompt": "确认执行方案",
            "decisions": ["approve", "reject"],
            "draft_schema": "agent-team-review-v1",
            "draft": {"goal": "交付报告"},
        }
    return {
        "tool_name": "agent_team",
        "status": "success",
        "content": '{"status": "pending"}',
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": "call-a",
        "display_data": {"kind": "agent-team-approval", "status": "pending"},
        "user_input_request": request,
    }


def _decision(kind: UserDecisionKind = UserDecisionKind.APPROVE) -> UserDecision:
    data = {"feedback": "目标太宽泛"} if kind is UserDecisionKind.REJECT else {}
    return UserDecision(request_id="12", kind=kind, data=data)


def test_extract_request_reads_declared_request_and_call_id() -> None:
    observation = ToolObservation(
        tool_name="agent_team",
        status="success",
        tool_call_id="call-1",
        user_input_request=UserInputRequest(request_id="17", kind="agent_team_review"),
    )
    projection = asdict(observation)

    request = extract_request([projection])

    assert request is not None
    assert request.request_id == "17"
    assert request_call_id([projection], request.request_id) == "call-1"


def test_extract_request_returns_none_without_declaration() -> None:
    assert extract_request([_observation(None)]) is None
    assert extract_request([{"tool_call_id": "call-x", "status": "success"}]) is None


def test_extract_request_rejects_multiple_pending_requests() -> None:
    second = _observation()
    second["tool_call_id"] = "call-b"
    with pytest.raises(ValueError, match="只能包含一个待决请求"):
        extract_request([_observation(), second])


def test_approval_clears_request_without_rewriting_tool_result() -> None:
    observations = [_observation()]

    (amended,) = apply_decision_to_observations(observations, _decision())

    assert amended["user_input_request"] is None
    assert amended["status"] == "success"
    assert observations[0]["user_input_request"] is not None


def test_rejection_marks_tool_result_cancelled_and_preserves_feedback() -> None:
    observations = [_observation()]

    (amended,) = apply_decision_to_observations(observations, _decision(UserDecisionKind.REJECT))

    assert amended["user_input_request"] is None
    assert amended["status"] == "cancelled"
    assert amended["display_data"]["user_decision"] == "reject"
    assert amended["display_data"]["status"] == "rejected"
    assert "目标太宽泛" in amended["reason"]


def test_unmatched_decision_returns_original_observations() -> None:
    observations = [_observation()]
    decision = UserDecision(request_id="missing", kind=UserDecisionKind.APPROVE)

    assert apply_decision_to_observations(observations, decision) is observations
