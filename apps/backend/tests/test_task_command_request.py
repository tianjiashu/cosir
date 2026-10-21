"""Task 级命令 wire schema：字段校验与到领域对象的映射。"""

import pytest
from pydantic import ValidationError

from app.assistant_transport.request import (
    EditMessageRequest,
    SendMessageRequest,
    UserDecisionRequest,
)
from app.models.user_decision import UserDecisionKind


def _message(text: str = "hello", **extra: object) -> dict[str, object]:
    return {"role": "user", "parts": [{"type": "text", "text": text}], **extra}


def _send(**overrides: object) -> SendMessageRequest:
    return SendMessageRequest.model_validate(
        {"message": _message(), "modelConfigId": 1, **overrides}
    )


def test_send_defaults_to_immediate_delivery_and_maps_command() -> None:
    request = _send(banTools=["read_file"])

    command = request.to_run_command()
    assert request.delivery == "immediate"
    assert command.display_text == "hello"
    assert command.ban_tools == ["read_file"]


@pytest.mark.parametrize("delivery", ["immediate", "after_run"])
def test_send_accepts_both_delivery_modes(delivery: str) -> None:
    assert _send(delivery=delivery).delivery == delivery


@pytest.mark.parametrize(
    "overrides",
    [
        {"delivery": "later"},
        {"banTools": ["read_file", "read_file"]},
        {"banTools": [" "]},
        {"runId": 1},
        {"modelConfigId": 0},
    ],
)
def test_send_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _send(**overrides)


def test_edit_rejects_delivery() -> None:
    """编辑只针对已到终态的 Task，不存在投递方式。"""
    with pytest.raises(ValidationError):
        EditMessageRequest.model_validate(
            {"message": _message(), "modelConfigId": 1, "delivery": "immediate"}
        )


def test_ordinary_file_attachment_maps_into_command() -> None:
    request = _send(
        message=_message(
            "查看 [[cosir-file:file-1]]",
            attachments=[
                {
                    "id": "file-1",
                    "name": "notes.md",
                    "contentType": "text/markdown",
                    "path": "C:/workspace/notes.md",
                }
            ],
        )
    )

    [attachment] = request.to_run_command().attachments
    assert attachment.id == "file-1"
    assert attachment.content_type == "text/markdown"


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
def test_reasoning_effort_uses_run_domain_values(effort: str | None) -> None:
    assert _send(reasoningEffort=effort).reasoningEffort == effort


@pytest.mark.parametrize("effort", ["medium", "", "HIGH", 1, True])
def test_invalid_reasoning_effort_is_rejected(effort: object) -> None:
    with pytest.raises(ValidationError, match="reasoningEffort"):
        _send(reasoningEffort=effort)


def test_decision_maps_to_domain() -> None:
    decision = UserDecisionRequest.model_validate(
        {"requestId": "req-1", "decision": "approve", "data": {"note": "ok"}}
    ).to_user_decision()

    assert decision.request_id == "req-1"
    assert decision.kind is UserDecisionKind.APPROVE
    assert decision.data == {"note": "ok"}


@pytest.mark.parametrize("decision", ["force_reject", "maybe"])
def test_decision_rejects_system_or_unknown_kinds(decision: str) -> None:
    """FORCE_REJECT 只由系统合成，客户端不能提交。"""
    with pytest.raises(ValidationError):
        UserDecisionRequest.model_validate({"requestId": "req-1", "decision": decision})
