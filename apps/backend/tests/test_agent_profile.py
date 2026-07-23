"""Agent profile serialization tests."""

import json

from app.core.agents.agent_profile import default_developer_agent
from app.models.enums.event_type import EventType
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
        payload={"agent": profile},
    )

    json.dumps(profile)
    json.dumps(event.to_dict())

    assert profile["allowed_tools"] == ["safe_read"]
    assert profile["workflow"] == "react_like_v1"
    assert "model_settings" not in profile
    assert "api_key_env" not in profile
