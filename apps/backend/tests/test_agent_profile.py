"""Agent profile serialization tests."""

import json

import pytest

from app.core.agents.agent_profile import default_developer_agent
from app.models.enums.event_type import EventType
from app.models.payload import RunFailedPayload, RunStartedPayload
from app.models.payload.registry import EVENT_PAYLOAD_MODELS
from app.models.runtime_event import RuntimeEvent


def test_agent_profile_to_dict_is_json_serializable() -> None:
    """Ensure Agent profile dictionaries can be emitted through JSON/SSE boundaries.

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If the serialized profile contains non-JSON values.

    Side effects:
        None.
    """

    profile = default_developer_agent().to_dict()
    event = RuntimeEvent(
        event_type=EventType.RUN_STARTED,
        task_id="task-1",
        turn_id="turn-1",
        payload=RunStartedPayload(status="running", agent_id=profile),
    )

    json.dumps(profile)
    json.dumps(event.to_dict())

    assert profile["allowed_tools"] == ["read_file"]
    assert profile["workflow"] == "react_like_v1"
    assert "model_settings" not in profile
    assert "api_key_env" not in profile


def test_runtime_event_payload_is_normalized_by_event_type() -> None:
    """运行时事件按事件类型规范化 payload。

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If optional ``None`` fields are not removed from the emitted payload.

    Side effects:
        None.
    """

    event = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id="task-1",
        payload=RunFailedPayload(error="boom", status=None, step_id=None),
    )

    assert event.to_dict()["payload"] == {"error": "boom"}


def test_runtime_event_rejects_naked_payload_dict() -> None:
    """运行时事件拒绝裸字典 payload。

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If a naked payload dictionary is accepted.

    Side effects:
        None.
    """

    with pytest.raises(TypeError):
        RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id="task-1",
            payload={"status": "completed"},  # type: ignore[arg-type]
        )


def test_runtime_event_rejects_mismatched_payload_entity() -> None:
    """运行时事件拒绝与事件类型不匹配的 payload 实体。

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If a mismatched payload entity is accepted.

    Side effects:
        None.
    """

    with pytest.raises(TypeError):
        RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id="task-1",
            payload=RunStartedPayload(status="running", agent_id={}),
        )


def test_all_event_types_have_payload_models() -> None:
    """所有事件类型都必须登记 payload 模型。

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If an ``EventType`` is missing from the payload registry.

    Side effects:
        None.
    """

    assert set(EVENT_PAYLOAD_MODELS) == set(EventType)


def test_all_event_payload_models_accept_minimal_examples() -> None:
    """每种事件 payload 模型都接受一个最小合法样例。

    Parameters:
        None.

    Returns:
        None.

    Raises:
        AssertionError: If any registered payload model rejects its minimal event sample.

    Side effects:
        None.
    """

    examples: dict[EventType, dict[str, object]] = {
        EventType.RUN_STARTED: {"status": "running", "agent": {}},
        EventType.RUN_FAILED: {"error": "boom"},
        EventType.RUN_CANCELLED: {"status": "cancelled"},
        EventType.RUN_FINISHED: {"status": "completed"},
        EventType.STEP_STARTED: {"step_id": "step-1", "kind": "model", "index": 1},
        EventType.MODEL_REQUESTED: {"step_id": "step-1", "message_count": 1},
        EventType.MODEL_OUTPUT_DELTA: {"step_id": "step-1", "text": "hello"},
        EventType.MODEL_THINKING_DELTA: {"step_id": "step-1", "text": "thinking"},
        EventType.MODEL_COMPLETED: {"step_id": "step-1", "text": "done", "tool_calls": []},
        EventType.MODEL_FAILED: {"error": "boom"},
        EventType.TOOL_CALL_REQUESTED: {"tool_name": "read_file"},
        EventType.TOOL_CALL_STARTED: {"tool_name": "read_file"},
        EventType.TOOL_CALL_FINISHED: {
            "step_id": "step-1",
            "tool_name": "read_file",
            "status": "success",
            "tool_call_id": "call-1",
        },
        EventType.OBSERVATION_ADDED: {"tool_name": "read_file", "status": "success"},
        EventType.FINAL_RESPONSE: {
            "text": "done",
            "step_id": "step-1",
            "status": "completed",
        },
        EventType.HUMAN_INPUT_REQUESTED: {"prompt": "continue?"},
        EventType.HUMAN_INPUT_RECEIVED: {},
    }

    assert set(examples) == set(EventType)
    for event_type, payload in examples.items():
        payload_entity = EVENT_PAYLOAD_MODELS[event_type].model_validate(payload)
        event = RuntimeEvent(event_type=event_type, task_id="task-1", payload=payload_entity)
        assert event.event_type is event_type
        assert event.payload is payload_entity
