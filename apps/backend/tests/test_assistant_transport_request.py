from app.assistant_transport.request.assistant_transport_request import AssistantTransportRequest


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
