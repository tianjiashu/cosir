"""Task 级命令 wire schema：字段校验与到领域对象的映射。"""

import pytest
from pydantic import ValidationError

from app.assistant_transport.request import (
    EditMessageRequest,
    SendMessageRequest,
    TeamDecisionRequest,
)


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


@pytest.mark.parametrize("delivery", ["immediate", "next_run"])
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


def test_team_decision_addresses_team_run() -> None:
    decision = TeamDecisionRequest.model_validate(
        {"teamRunId": 17, "decision": "approve", "data": {"goal": "ok"}}
    )

    assert (decision.teamRunId, decision.decision, decision.data) == (17, "approve", {"goal": "ok"})


@pytest.mark.parametrize(
    "payload",
    [
        {"teamRunId": 17, "decision": "force_reject"},
        {"teamRunId": 17, "decision": "abort"},
        {"teamRunId": 0, "decision": "approve"},
        {"requestId": "17", "decision": "approve"},
    ],
)
def test_team_decision_rejects_unknown_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TeamDecisionRequest.model_validate(payload)
