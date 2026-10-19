"""Disabled tool closure projection regression tests."""

import json

import pytest

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
)
from app.models.message_metadata import ToolMessageMetadata


def test_rebuild_pairs_disabled_tool_closure_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """禁用调用的 ToolMessage 参与冷重建时，必须有完整的状态 metadata。"""

    class _ToolRegistry:
        def get_tool_definition(self, _name: str):
            return None

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _ToolRegistry(),
    )
    rows = [
        ConversationTaskContextRecord(
            id=1,
            task_id=7,
            run_id=1,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "disabled-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({})},
                    }
                ],
            },
            include_in_context=True,
            sequence=1,
        ),
        ConversationTaskContextRecord(
            id=2,
            task_id=7,
            run_id=1,
            message={
                "role": "tool",
                "content": "This tool is disabled for the current run.",
                "tool_call_id": "disabled-1",
            },
            include_in_context=True,
            sequence=2,
            metadata=ToolMessageMetadata(status="cancelled"),
        ),
    ]

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)

    assert parts["disabled-1"]["status"] == "cancelled"
    assert parts["disabled-1"]["isError"] is False
