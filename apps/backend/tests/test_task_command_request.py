"""Task 级命令 wire schema：字段校验与到领域对象的映射。"""

import pytest
from pydantic import ValidationError

from app.assistant_transport.request import (
    RerunMessageRequest,
    SendMessageRequest,
    DecisionRequest,
)


def _message(text: str = "hello", **extra: object) -> dict[str, object]:
    return {"role": "user", "parts": [{"type": "text", "text": text}], **extra}


def _send(**overrides: object) -> SendMessageRequest:
    return SendMessageRequest.model_validate(
        {"message": _message(), "modelConfigId": 1, **overrides}
    )


def test_send_maps_command() -> None:
    request = _send(disabledToolGroups=["联网工具"])

    command = request.to_run_command()
    assert command.display_text == "hello"
    assert command.disabled_tool_groups == ["联网工具"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"delivery": "immediate"},
        {"disabledToolGroups": ["联网工具", "联网工具"]},
        {"disabledToolGroups": [" "]},
        {"runId": 1},
        {"modelConfigId": 0},
    ],
)
def test_send_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _send(**overrides)


def test_rerun_rejects_delivery() -> None:
    """重跑只针对已停下的 Task（运行中先停止），不存在投递方式。"""
    with pytest.raises(ValidationError):
        RerunMessageRequest.model_validate(
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


def test_decision_carries_no_tool_specific_address() -> None:
    """调用由路径里的 tool_call_id 寻址，请求体只有决定本身。"""
    decision = DecisionRequest.model_validate(
        {"decision": "reject", "feedback": "换个分工", "data": {"goal": "ok"}}
    )

    assert (decision.decision, decision.feedback, decision.data) == (
        "reject", "换个分工", {"goal": "ok"}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "force_reject"},
        {"decision": "abort"},
        {"teamRunId": 17, "decision": "approve"},
    ],
)
def test_decision_rejects_unknown_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(payload)
