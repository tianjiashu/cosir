"""human-in-the-loop 纯投影函数的契约测试。

覆盖：从工具观察派生待决请求、建立「请求 ↔ 调用」关联、解析外部恢复值、把决定写回观察。
全部为纯函数，不依赖 graph / 数据库 / 模型。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from app.core.tools.schemas.tool_observation import ToolObservation
from app.core.tools.schemas.user_decision import UserDecision, UserDecisionKind
from app.core.tools.schemas.user_input_request import UserInputRequest
from app.core.workflows.react.node_helper.user_input_projection import (
    apply_decisions_to_observations,
    extract_requests,
    parse_resume_decisions,
    request_call_ids,
)


def _observation(
    *,
    tool_call_id: str = "call-a",
    request_id: str = "12",
    request_overrides: dict[str, Any] | None = None,
    request: object = ...,
) -> dict[str, Any]:
    """构造一条带待决声明的工具观察摘要（形状与 ``dataclasses.asdict`` 投影一致）。"""

    if request is ...:
        payload: dict[str, Any] = {
            "kind": "agent_team_review",
            "request_id": request_id,
            "prompt": "确认执行方案",
            "decisions": ["approve", "reject"],
            "draft_schema": "agent-team-review-v1",
            "draft": {"goal": "交付报告"},
        }
        payload.update(request_overrides or {})
        request = payload
    return {
        "tool_name": "agent_team",
        "status": "success",
        "content": '{"status": "pending"}',
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": tool_call_id,
        "display_data": {"kind": "agent-team-preview", "status": "pending"},
        "user_input_request": request,
    }


def _decision(
    request_id: str = "12",
    kind: UserDecisionKind = UserDecisionKind.APPROVE,
    data: dict[str, Any] | None = None,
) -> UserDecision:
    """构造一条用户决定。"""

    return UserDecision(request_id=request_id, kind=kind, data=data or {})


# --- extract_requests ------------------------------------------------------------------


def test_extract_requests_reads_declared_request_with_default_decisions() -> None:
    """未声明 decisions 的请求按「批准 / 驳回」最小集合解释。"""

    (request,) = extract_requests([_observation(request_overrides={"decisions": None})])

    assert request.request_id == "12"
    assert request.kind == "agent_team_review"
    assert request.draft == {"goal": "交付报告"}
    assert request.draft_schema == "agent-team-review-v1"
    assert list(request.decisions) == [UserDecisionKind.APPROVE, UserDecisionKind.REJECT]


def test_extract_requests_accepts_observation_asdict_projection() -> None:
    """真实链路形状：观察经 ``dataclasses.asdict`` 投影后仍能被派生。

    这条用例钉住两件事：字段名（``user_input_request``）与元组形状（``asdict`` 把
    ``decisions`` 投影成元组、枚举元素保持不变），两者都是与 workflow state 之间的契约。
    """

    observation = ToolObservation(
        tool_name="agent_team",
        status="success",
        tool_call_id="call-1",
        user_input_request=UserInputRequest(request_id="17", kind="agent_team_review"),
    )

    projection = asdict(observation)
    assert projection["user_input_request"] is not None

    (request,) = extract_requests([projection])

    assert request.request_id == "17"
    assert list(request.decisions) == [UserDecisionKind.APPROVE, UserDecisionKind.REJECT]
    assert request_call_ids([projection]) == {"17": "call-1"}


def test_extract_requests_ignores_observations_without_declaration() -> None:
    """没有声明的观察不产生待决请求（已作答 / 普通工具结果都不受影响）。"""

    assert extract_requests([{"tool_call_id": "call-x", "status": "success"}]) == []
    # 声明被清空（物化后的形状）同样不产生请求。
    assert extract_requests([_observation(request=None)]) == []


def test_extract_requests_requires_request_id_and_kind() -> None:
    """缺少关键字段必须显式失败：否则决定无法匹配回请求。"""

    malformed = _observation(request={"kind": "agent_team_review"})
    with pytest.raises(ValueError, match="request_id"):
        extract_requests([malformed])

    without_kind = _observation(request={"request_id": "12"})
    with pytest.raises(ValueError, match="kind"):
        extract_requests([without_kind])

    not_mapping = _observation(request="approve")
    with pytest.raises(ValueError, match="必须是对象"):
        extract_requests([not_mapping])


def test_extract_requests_rejects_duplicate_request_id_in_one_batch() -> None:
    """同批重复 request_id 会让决定匹配二义，必须拒绝。"""

    with pytest.raises(ValueError, match="重复的 request_id"):
        extract_requests([_observation(), _observation(tool_call_id="call-b")])


def test_extract_requests_rejects_unknown_declared_decision() -> None:
    """工具声明了框架不认识的决定时必须报错，而不是静默忽略该声明。"""

    with pytest.raises(ValueError, match="未知的决定种类声明"):
        extract_requests([_observation(request_overrides={"decisions": ["approve", "maybe"]})])


# --- request_call_ids ------------------------------------------------------------------


def test_request_call_ids_maps_request_to_owning_call() -> None:
    """观察是「请求」与「调用」的接合点：批准后要重开哪条调用由它给出。"""

    mapping = request_call_ids(
        [_observation(), _observation(tool_call_id="call-b", request_id="13")]
    )

    assert mapping == {"12": "call-a", "13": "call-b"}


def test_request_call_ids_skips_observations_without_declaration() -> None:
    """没有声明的观察（已作答 / 普通结果）不参与关联。"""

    assert request_call_ids([{"tool_call_id": "call-x"}]) == {}


def test_request_call_ids_requires_tool_call_id() -> None:
    """声明了请求却没有 tool_call_id 时必须失败：否则会变成「用户批了却没执行」。"""

    broken = _observation()
    broken.pop("tool_call_id")

    with pytest.raises(ValueError, match="tool_call_id"):
        request_call_ids([broken])


# --- parse_resume_decisions ------------------------------------------------------------


def test_parse_resume_decisions_accepts_empty_and_none() -> None:
    """空恢复值表示「用户尚未作答」，不是错误。"""

    assert parse_resume_decisions(None) == []
    assert parse_resume_decisions({"decisions": []}) == []


def test_parse_resume_decisions_maps_wire_values_to_domain() -> None:
    (decision,) = parse_resume_decisions(
        {"decisions": [{"request_id": "12", "decision": "reject", "data": {"feedback": "太宽泛"}}]}
    )

    assert decision.request_id == "12"
    assert decision.kind is UserDecisionKind.REJECT
    assert decision.data == {"feedback": "太宽泛"}


@pytest.mark.parametrize(
    "payload",
    [
        "approve",
        {"decisions": "approve"},
        {"decisions": [{"decision": "approve"}]},
        {"decisions": [{"request_id": "12"}]},
        {"decisions": [{"request_id": "12", "decision": "maybe"}]},
    ],
)
def test_parse_resume_decisions_rejects_malformed_payload(payload: object) -> None:
    """进程外输入的形状错误必须在边界上抛，不向下游扩散宽容解析。"""

    with pytest.raises(ValueError):
        parse_resume_decisions(payload)


# --- apply_decisions_to_observations ---------------------------------------------------


def test_apply_decisions_returns_same_object_when_no_decision_matches() -> None:
    """没有决定命中任何观察时原样返回入参，使调用方可以据此跳过无意义的 state 写入。"""

    observations = [_observation()]

    assert apply_decisions_to_observations(observations, []) is observations
    assert (
        apply_decisions_to_observations(observations, [_decision(request_id="999")])
        is observations
    )


def test_approval_clears_declaration_without_rewriting_result() -> None:
    """批准只清掉待决声明：真实结果由重执行产出并覆盖旧观察，不在此时改写成取消。"""

    observations = [_observation()]

    (amended,) = apply_decisions_to_observations(observations, [_decision()])

    assert amended is not observations[0]
    assert amended["user_input_request"] is None
    assert amended["status"] == "success"
    assert amended["content"] == '{"status": "pending"}'
    assert amended["display_data"] == observations[0]["display_data"]
    # 原观察不被就地修改（避免 state 与调用记录悄悄共享同一份可变数据）。
    assert observations[0]["user_input_request"] is not None


def test_rejection_rewrites_observation_as_cancelled_with_feedback() -> None:
    """驳回改写为取消终态、携带用户意见、清掉待决声明，并在展示数据上标注用户决定。"""

    observations = [_observation()]
    amendment = _decision(kind=UserDecisionKind.REJECT, data={"feedback": "目标太宽泛"})

    (amended,) = apply_decisions_to_observations(observations, [amendment])

    assert amended["status"] == "cancelled"
    assert amended["content"] == ""
    assert "目标太宽泛" in amended["reason"]
    assert amended["user_input_request"] is None
    assert amended["display_data"]["status"] == "rejected"
    assert amended["display_data"]["user_decision"] == "reject"
    assert amended["display_data"]["user_input"] == {"feedback": "目标太宽泛"}
    assert observations[0]["status"] == "success"


def test_abort_uses_cancelled_semantics_without_retry_hint() -> None:
    """放弃同样走取消语义（不计入连续失败），文案与驳回可区分。"""

    (amended,) = apply_decisions_to_observations(
        [_observation()], [_decision(kind=UserDecisionKind.ABORT)]
    )

    assert amended["status"] == "cancelled"
    assert amended["retryable"] is False
    assert amended["user_input_request"] is None
    assert "aborted" in amended["reason"]
    assert amended["display_data"]["status"] == "aborted"


def test_only_decided_observations_are_rewritten() -> None:
    """同批多条请求只有被作答的那些被清声明，其余保持待决（供自环重新挂起）。"""

    observations = [
        _observation(),
        _observation(tool_call_id="call-b", request_id="13"),
    ]

    amended = apply_decisions_to_observations(observations, [_decision()])

    assert amended[0]["user_input_request"] is None
    assert amended[1]["user_input_request"] is not None
    assert [request.request_id for request in extract_requests(amended)] == ["13"]
