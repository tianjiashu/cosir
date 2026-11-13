"""Disabled tool closure projection regression tests."""

import json

import pytest

from app.assistant_transport.projection.snapshot_rebuild import (
    pair_tool_parts,
)
from app.models.session_context import (
    SessionContextRecord,
)
from app.models.message_metadata import ToolMessageMetadata


def test_rebuild_pairs_disabled_tool_closure_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """禁用调用的 ToolMessage 参与冷重建时，必须有完整的状态 metadata。"""

    rows = [
        SessionContextRecord(
            id=1,
            session_id=7,
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
            turn=1,
        ),
        SessionContextRecord(
            id=2,
            session_id=7,
            run_id=1,
            message={
                "role": "tool",
                "content": "This tool is disabled for the current run.",
                "tool_call_id": "disabled-1",
            },
            include_in_context=True,
            sequence=2,
            metadata=ToolMessageMetadata(status="cancelled"),
            turn=1,
        ),
    ]

    parts = pair_tool_parts(rows, {})

    assert parts["disabled-1"]["status"] == "cancelled"
    assert parts["disabled-1"]["isError"] is False
