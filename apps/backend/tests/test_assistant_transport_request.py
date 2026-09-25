import pytest
from pydantic import ValidationError

from app.assistant_transport.request.assistant_transport_request import (
    AssistantTransportRequest,
    TransportRequestError,
)
from app.assistant_transport.request.command.ban_tools_command import BanToolsCommand


def _request(**extra: object) -> AssistantTransportRequest:
    commands = extra.pop("commands", None)
    if commands is None:
        commands = [
            {
                "type": "add-message",
                "commandId": "command-1",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
                "parentId": None,
                "sourceId": None,
            }
        ]
    return AssistantTransportRequest(
        commands=commands,
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


def test_accepts_add_message_with_run_id_without_source_id() -> None:
    request = AssistantTransportRequest(
        commands=[
            {
                "type": "add-message",
                "commandId": "replay-command-1",
                "message": {"role": "user", "parts": [{"type": "text", "text": "replay"}]},
                "sourceId": None,
            }
        ],
        threadId="task-1",
        taskId=1,
        providerId=1,
        modelName="deepseek-chat",
        runId=42,
    )

    assert request.runId == 42


def test_source_id_does_not_change_payload_hash() -> None:
    first = _request()
    second = _request()
    second.commands[0].sourceId = "user-1"

    assert first.payload_hash() == second.payload_hash()


def test_run_id_changes_payload_hash() -> None:
    first = _request()
    second = _request(runId=42)

    assert first.payload_hash() != second.payload_hash()


def test_accepts_typed_ban_tools_command_and_hashes_its_payload() -> None:
    payload = {
        "type": "custom",
        "commandId": "command-ban-tools",
        "name": "ban-tools",
        "payload": {"ban_tools": ["read_file"]},
    }
    request = _request(commands=[
        {
            "type": "add-message",
            "commandId": "command-message",
            "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        },
        payload,
    ])
    changed = _request(commands=[
        {
            "type": "add-message",
            "commandId": "command-message",
            "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        },
        {**payload, "payload": {"ban_tools": ["write_file"]}},
    ])

    assert request.payload_hash() != changed.payload_hash()
    assert isinstance(request.commands[1], BanToolsCommand)
    command = request.commands[1]
    assert isinstance(command, BanToolsCommand)
    assert command.payload.ban_tools == ["read_file"]


def test_payload_hash_is_independent_of_command_and_tool_selection_order() -> None:
    add_message = {
        "type": "add-message",
        "commandId": "command-message",
        "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
    }
    ban_tools = {
        "type": "custom",
        "commandId": "command-ban-tools",
        "name": "ban-tools",
        "payload": {"ban_tools": ["read_file", "write_file"]},
    }
    first = _request(commands=[add_message, ban_tools])
    reordered = _request(
        commands=[
            {**ban_tools, "payload": {"ban_tools": ["write_file", "read_file"]}},
            add_message,
        ]
    )

    assert first.payload_hash() == reordered.payload_hash()


def test_accepts_empty_typed_ban_tools_selection() -> None:
    request = _request(commands=[
        {
            "type": "add-message",
            "commandId": "command-message",
            "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        },
        {
            "type": "custom",
            "commandId": "command-ban-tools",
            "name": "ban-tools",
            "payload": {"ban_tools": []},
        },
    ])

    command = request.commands[1]
    assert isinstance(command, BanToolsCommand)
    assert command.payload.ban_tools == []


def test_rejects_unregistered_custom_command() -> None:
    with pytest.raises(ValidationError):
        _request(commands=[
            {
                "type": "add-message",
                "commandId": "command-message",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
            },
            {
                "type": "custom",
                "commandId": "command-custom",
                "name": "unknown",
                "payload": {},
            },
        ])


@pytest.mark.parametrize(
    "payload",
    [
        {"ban_tools": ["read_file", "read_file"]},
        {"ban_tools": [""]},
        {"ban_tools": ["read_file"], "other": True},
    ],
)
def test_rejects_malformed_ban_tools_payload(payload: object) -> None:
    with pytest.raises(ValidationError):
        _request(commands=[
            {
                "type": "add-message",
                "commandId": "command-message",
                "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
            },
            {
                "type": "custom",
                "commandId": "command-ban-tools",
                "name": "ban-tools",
                "payload": payload,
            },
        ])


def test_accepts_explicit_ordinary_file_attachment_metadata() -> None:
    request = AssistantTransportRequest(
        commands=[{
            "type": "add-message",
            "commandId": "command-file-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "查看 [[cosir-file:file-1]]"}],
                "attachments": [{
                    "id": "file-1",
                    "name": "notes.md",
                    "contentType": "text/markdown",
                    "path": "C:/workspace/notes.md",
                }],
            },
        }],
        threadId="task-1",
        taskId=1,
        providerId=1,
        modelName="deepseek-chat",
    )

    assert request.commands[0].message.attachments[0].id == "file-1"
