"""Task 级命令 wire schema：字段校验与到领域对象的映射。"""

import pytest
from pydantic import ValidationError

from app.assistant_transport.request import DecisionRequest, SendMessageRequest


def _message(text: str = "hello", **extra: object) -> dict[str, object]:
    return {"role": "user", "parts": [{"type": "text", "text": text}], **extra}


def _send(**overrides: object) -> SendMessageRequest:
    return SendMessageRequest.model_validate(
        {"message": _message(), "modelConfigId": 1, **overrides}
    )


def test_send_maps_command() -> None:
    request = _send(disabledToolGroups=["联网工具"])

    command = request.to_run_command()
    assert command.text == "hello"
    assert command.disabled_tool_groups == ["联网工具"]


def test_text_parts_are_joined_and_images_map_to_asset_ids() -> None:
    image_id = "a" * 64
    request = SendMessageRequest.model_validate(
        {
            "message": {
                "role": "user",
                "parts": [
                    {"type": "text", "text": "前"},
                    {
                        "type": "image",
                        "image": f"cosir-attachment://{image_id}",
                    },
                    {"type": "text", "text": "后"},
                ],
            },
            "modelConfigId": 1,
        }
    )

    command = request.to_run_command()

    assert command.text == "前\n后"
    assert command.image_asset_ids == [image_id]


@pytest.mark.parametrize(
    "overrides",
    [
        {"delivery": "immediate"},
        {"disabledToolGroups": ["联网工具", "联网工具"]},
        {"disabledToolGroups": [" "]},
        {"runId": 1},
        {"modelConfigId": 0},
        # 附件不再有独立字段：本机文件与工作区引用是正文里的胶囊。
        {"message": _message(attachments=[])},
    ],
)
def test_send_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _send(**overrides)


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
        "reject",
        "换个分工",
        {"goal": "ok"},
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
