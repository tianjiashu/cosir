"""用户决定命令（wire 契约）与续跑源判定的测试。

覆盖：命令的结构约束（必须单独提交、携带 runId、requestId 唯一）、wire → 领域映射，
以及 ``resume_run`` 允许的续跑源状态判定。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.assistant_transport.request import (
    AssistantTransportRequest,
    TransportRequestError,
    UserInputDecisionCommand,
)
from app.assistant_transport.service.conversation_run_command_service import _resumable_status
from app.core.tools.schemas.user_decision import (
    UserDecision,
    UserDecisionKind,
    build_resume_payload,
)
from app.core.workflows.react.node_helper.user_input_projection import parse_resume_decisions
from app.models.enums.conversation_run_status import ConversationRunStatus


def _request(commands: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    """构造最小合法 Transport 请求。"""

    payload: dict[str, object] = {
        "commands": commands,
        "threadId": "task-7",
        "taskId": 7,
        **overrides,
    }
    return payload


def _decision_command(*, request_id: str = "12", decision: str = "approve") -> dict[str, object]:
    """构造一条用户决定命令。"""

    return {
        "type": "custom",
        "commandId": "cmd-1",
        "name": "user-input-decision",
        "payload": {
            "decisions": [
                {"request_id": request_id, "decision": decision, "data": {"goal": "交付报告"}},
            ]
        },
    }


def test_decision_command_parses_and_maps_to_domain() -> None:
    """决定命令在 wire 边界完成校验并映射为领域值对象。"""

    request = AssistantTransportRequest.model_validate(
        _request([_decision_command()], runId=31)
    )

    (command,) = request.commands
    assert isinstance(command, UserInputDecisionCommand)
    (decision,) = command.to_user_decisions()
    assert decision.request_id == "12"
    assert decision.kind is UserDecisionKind.APPROVE
    assert decision.data == {"goal": "交付报告"}


def test_decision_command_requires_run_id() -> None:
    """没有 runId 时无法定位要恢复的 Run，必须在解析期拒绝。"""

    with pytest.raises(TransportRequestError) as raised:
        AssistantTransportRequest.model_validate(_request([_decision_command()]))

    assert raised.value.code == "RUN_ID_REQUIRED"


def test_decision_command_must_be_submitted_alone() -> None:
    """决定与新消息同批会把「恢复」和「新建 / 编辑」两种 Run 模式混在一次请求里。"""

    with pytest.raises(TransportRequestError) as raised:
        AssistantTransportRequest.model_validate(
            _request(
                [
                    {
                        "type": "add-message",
                        "commandId": "cmd-2",
                        "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                    },
                    _decision_command(),
                ],
                runId=31,
                modelConfigId=2,
            )
        )

    assert raised.value.code == "USER_DECISION_COMMAND_INVALID"


def test_multiple_decision_commands_are_rejected() -> None:
    """一次请求最多一个决定命令：两个命令的语义（覆盖 / 合并）没有定义。"""

    other = _decision_command(request_id="13")
    other["commandId"] = "cmd-3"

    with pytest.raises(TransportRequestError) as raised:
        AssistantTransportRequest.model_validate(
            _request([_decision_command(), other], runId=31)
        )

    assert raised.value.code == "MULTIPLE_USER_DECISION_COMMANDS"


def test_duplicate_request_id_in_one_payload_is_rejected() -> None:
    """同一请求在一次提交里给两个决定会让「谁生效」不确定，直接在 wire 层拒绝。"""

    duplicated = {
        "type": "custom",
        "commandId": "cmd-1",
        "name": "user-input-decision",
        "payload": {
            "decisions": [
                {"request_id": "12", "decision": "approve"},
                {"request_id": "12", "decision": "reject"},
            ]
        },
    }

    with pytest.raises(ValidationError):
        AssistantTransportRequest.model_validate(_request([duplicated], runId=31))


def test_unknown_decision_kind_is_rejected_by_wire_schema() -> None:
    """未声明的决定种类由 wire schema 拒绝，不留到图执行期。"""

    with pytest.raises(ValidationError):
        AssistantTransportRequest.model_validate(
            _request([_decision_command(decision="maybe")], runId=31)
        )


def test_resume_payload_round_trips_through_node_parser() -> None:
    """workflow 产出的恢复载荷必须能被等待节点解析。

    这是真实批准链路的唯一联结点：``workflow`` 用 ``build_resume_payload`` 造载荷、
    ``wait_user`` 用 ``parse_resume_decisions`` 解析。两处各维护一份键名（``kind`` vs
    ``decision``）时，测试可以各自自洽而线上 100% 失败，因此必须锁死往返一致。
    """

    decisions = (
        UserDecision(request_id="12", kind=UserDecisionKind.APPROVE, data={"goal": "交付报告"}),
        UserDecision(request_id="13", kind=UserDecisionKind.REJECT),
    )

    payload = build_resume_payload(decisions)

    assert payload == {
        "decisions": [
            {"request_id": "12", "decision": "approve", "data": {"goal": "交付报告"}},
            {"request_id": "13", "decision": "reject", "data": {}},
        ]
    }
    assert parse_resume_decisions(payload) == list(decisions)
    assert build_resume_payload(()) == {"decisions": []}


def test_resumable_status_maps_waiting_and_cancelled_only() -> None:
    """只有「等待用户决定」与「已取消」能续跑同一 checkpoint。"""

    assert (
        _resumable_status(ConversationRunStatus.WAITING_FOR_INPUT.value)
        is ConversationRunStatus.WAITING_FOR_INPUT
    )
    assert (
        _resumable_status(ConversationRunStatus.CANCELLED.value)
        is ConversationRunStatus.CANCELLED
    )
    for status in (
        ConversationRunStatus.PENDING.value,
        ConversationRunStatus.RUNNING.value,
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.FAILED.value,
    ):
        assert _resumable_status(status) is None
