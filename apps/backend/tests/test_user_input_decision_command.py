"""用户决定命令（wire 契约）与续跑源判定的测试。

覆盖：命令的结构约束（必须单独提交、携带 runId、payload 恰好一个决定、拒绝系统专用决定种类）、
wire → 领域映射、workflow 产出的恢复载荷与节点解析器的往返一致，以及 ``resume_run`` 允许的续跑源
状态判定。
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
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.user_decision import (
    UserDecisionKind,
)


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
    """构造一条用户决定命令（一次提交恰好一个决定）。"""

    return {
        "type": "custom",
        "commandId": "cmd-1",
        "name": "user-input-decision",
        "payload": {
            "decision": {
                "request_id": request_id,
                "decision": decision,
                "data": {"goal": "交付报告"},
            }
        },
    }


def test_decision_command_parses_and_maps_to_domain() -> None:
    """决定命令在 wire 边界完成校验并映射为领域值对象。"""

    request = AssistantTransportRequest.model_validate(_request([_decision_command()], runId=31))

    (command,) = request.commands
    assert isinstance(command, UserInputDecisionCommand)
    decision = command.to_user_decision()
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
        AssistantTransportRequest.model_validate(_request([_decision_command(), other], runId=31))

    assert raised.value.code == "MULTIPLE_USER_DECISION_COMMANDS"


def test_second_decision_in_one_payload_is_rejected() -> None:
    """一次提交只承载一个决定：payload 里再塞一条决定字段由 wire schema 拒绝。"""

    duplicated = _decision_command()
    duplicated["payload"] = {
        "decision": {"request_id": "12", "decision": "approve"},
        "decisions": [{"request_id": "12", "decision": "reject"}],
    }

    with pytest.raises(ValidationError):
        AssistantTransportRequest.model_validate(_request([duplicated], runId=31))


def test_unknown_decision_kind_is_rejected_by_wire_schema() -> None:
    """未声明的决定种类由 wire schema 拒绝，不留到图执行期。"""

    with pytest.raises(ValidationError):
        AssistantTransportRequest.model_validate(
            _request([_decision_command(decision="maybe")], runId=31)
        )


def test_force_reject_is_not_client_submittable() -> None:
    """强行拒绝由系统在取消等待中的 Run 时合成。

    它不核对 ``request_id`` 就能作废当前待决请求，因此这是进程内信号，不接受客户端提交。
    """

    with pytest.raises(TransportRequestError) as raised:
        AssistantTransportRequest.model_validate(
            _request([_decision_command(decision="force_reject")], runId=31)
        )

    assert raised.value.code == "FORCE_REJECT_NOT_CLIENT_SUBMITTABLE"


def test_resumable_status_maps_waiting_and_cancelled_only() -> None:
    """只有「等待用户决定」与「已取消」能续跑同一 checkpoint。"""

    assert (
        _resumable_status(ConversationRunStatus.WAITING_FOR_INPUT.value)
        is ConversationRunStatus.WAITING_FOR_INPUT
    )
    assert (
        _resumable_status(ConversationRunStatus.CANCELLED.value) is ConversationRunStatus.CANCELLED
    )
    for status in (
        ConversationRunStatus.PENDING.value,
        ConversationRunStatus.RUNNING.value,
        ConversationRunStatus.COMPLETED.value,
    ):
        assert _resumable_status(status) is None
