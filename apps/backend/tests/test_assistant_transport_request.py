import pytest

from app.assistant_transport.request.assistant_transport_request import (
    AssistantTransportRequest,
    TransportRequestError,
)


def _request(**extra: object) -> AssistantTransportRequest:
    return AssistantTransportRequest(
        commands=[
            {
                "type": "add-message",
                "commandId": "command-1",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
                "parentId": None,
                "sourceId": None,
            }
        ],
        threadId="task-1",
        taskId=1,
        providerId=1,
        modelName="deepseek-chat",
        **extra,
    )


def test_accepts_assistant_ui_compatibility_envelope() -> None:
    request = _request(
        parentId=None,
        state={"messages": []},
        system="",
        tools={"search": {"description": "search"}},
        callSettings={"temperature": 0.2},
        config={"mode": "default"},
    )

    assert request.parentId is None
    assert request.state == {"messages": []}
    assert request.callSettings == {"temperature": 0.2}


def test_rejects_unknown_root_field() -> None:
    try:
        _request(unknownAssistantField=True)
    except ValueError as error:
        assert "unknownAssistantField" in str(error)
    else:
        raise AssertionError("unknown root fields must remain forbidden")


def test_accepts_empty_commands_with_run_id_for_resume() -> None:
    request = AssistantTransportRequest(
        commands=[],
        threadId="task-1",
        taskId=1,
        runId="42",
    )

    assert request.runId == 42


def test_rejects_empty_commands_without_run_id() -> None:
    with pytest.raises(TransportRequestError) as error:
        AssistantTransportRequest(commands=[], threadId="task-1", taskId=1)
    assert error.value.code == "RUN_ID_REQUIRED"


def test_accepts_edit_command_with_source_id_and_current_run_id() -> None:
    request = AssistantTransportRequest(
        commands=[
            {
                "type": "add-message",
                "commandId": "edit-command-1",
                "message": {"role": "user", "parts": [{"type": "text", "text": "edited"}]},
                "sourceId": "user-1",
            }
        ],
        threadId="task-1",
        taskId=1,
        providerId=1,
        modelName="deepseek-chat",
        runId=42,
    )

    assert request.runId == 42
    assert request.commands[0].sourceId == "user-1"


def test_rejects_edit_command_without_run_id() -> None:
    with pytest.raises(TransportRequestError) as error:
        AssistantTransportRequest(
            commands=[
                {
                    "type": "add-message",
                    "commandId": "edit-command-1",
                    "message": {"role": "user", "parts": [{"type": "text", "text": "edited"}]},
                    "sourceId": "user-1",
                }
            ],
            threadId="task-1",
            taskId=1,
            providerId=1,
            modelName="deepseek-chat",
        )
    assert error.value.code == "EDIT_RUN_ID_REQUIRED"
