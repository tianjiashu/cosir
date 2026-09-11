from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    AgentContextLoader,
    ConversationStateRebuildError,
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord


def _timestamp() -> datetime:
    return datetime(2026, 9, 12, tzinfo=UTC)


def _task(task_id: int = 7) -> TaskRecord:
    timestamp = _timestamp()
    return TaskRecord(
        id=task_id,
        workspace_id=3,
        title="task",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _run(
    run_id: int = 11,
    *,
    task_id: int = 7,
    created_at: datetime | None = None,
    status: str = "completed",
    end_reason: str | None = "done",
    usage: dict[str, int | None] | None = None,
) -> ConversationRunRecord:
    timestamp = created_at or _timestamp()
    return ConversationRunRecord(
        id=run_id,
        task_id=task_id,
        input_text="must not become a UI message",
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        checkpoint_thread_id=f"thread-{run_id}",
        end_reason=end_reason,
        final_output="must not become a UI message",
        usage=usage,
    )


def _metadata(
    parts: list[dict[str, object]],
    *,
    tool_result: dict[str, object] | None = None,
    schema_version: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "parts": parts,
        "tool_result": tool_result,
    }


def _row(
    row_id: int,
    message: object,
    *,
    run_id: int | None = 11,
    task_id: int = 7,
    sequence: int = 1,
    include_in_context: bool = True,
    metadata: dict[str, object] | None = None,
    message_schema_version: int = 1,
) -> ConversationTaskContextRecord:
    return ConversationTaskContextRecord(
        id=row_id,
        task_id=task_id,
        run_id=run_id,
        message=message,
        include_in_context=include_in_context,
        sequence=sequence,
        transport_metadata=metadata or _metadata([]),
        message_schema_version=message_schema_version,
    )


def test_rebuild_empty_task_returns_valid_empty_snapshot() -> None:
    state = ConversationTaskStateRebuilder.rebuild(_task(), [], [])

    assert state == {
        "runs": [],
        "current_run_id": None,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }


def test_rebuild_maps_context_user_and_assistant_without_using_run_text() -> None:
    state = ConversationTaskStateRebuilder.rebuild(
        _task(),
        [_run()],
        [
            _row(101, HumanMessage(content="persisted user"), sequence=1),
            _row(
                102,
                AIMessage(content="persisted AI content is ignored"),
                sequence=2,
                metadata=_metadata(
                    [{"type": "text", "text": "persisted assistant", "status": "completed"}]
                ),
            ),
        ],
    )

    assert state["runs"] == [
        {
            "runId": 11,
            "status": "completed",
            "endReason": "done",
            "messages": [
                {
                    "id": "101",
                    "role": "user",
                    "parts": [{"type": "text", "text": "persisted user", "status": "completed"}],
                },
                {
                    "id": "102",
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "text": "persisted assistant", "status": "completed"}
                    ],
                },
            ],
            "usage": None,
        }
    ]


def test_rebuild_merges_ai_rows_in_sequence_using_first_row_id_and_keeps_part_order() -> None:
    state = ConversationTaskStateRebuilder.rebuild(
        _task(),
        [_run()],
        [
            _row(101, HumanMessage(content="question"), sequence=1),
            _row(
                203,
                AIMessage(content="first"),
                sequence=3,
                metadata=_metadata(
                    [{"type": "reasoning", "text": "think", "status": "completed"}]
                ),
            ),
            _row(
                202,
                AIMessage(content="second"),
                sequence=2,
                metadata=_metadata(
                    [{"type": "text", "text": "answer", "status": "completed"}]
                ),
            ),
        ],
    )

    assistant = state["runs"][0]["messages"][1]
    assert assistant["id"] == "202"
    assert assistant["parts"] == [
        {"type": "text", "text": "answer", "status": "completed"},
        {"type": "reasoning", "text": "think", "status": "completed"},
    ]


def test_rebuild_backfills_success_tool_result_and_deep_copies_display_data() -> None:
    tool_call = {
        "type": "tool-call",
        "toolCallId": "call-1",
        "toolName": "read_file",
        "status": "running",
        "args": {"path": "a.py"},
        "presentation": {"surface": "trace"},
        "isError": False,
    }
    result = {
        "status": "success",
        "display_data": {"kind": "file", "nested": {"items": ["a"]}},
        "status_hint": None,
        "error": None,
    }
    tool_row = _row(
        203,
        ToolMessage(content="ignored tool content", tool_call_id="call-1"),
        sequence=3,
        metadata=_metadata([], tool_result=result),
    )
    state = ConversationTaskStateRebuilder.rebuild(
        _task(),
        [_run()],
        [
            _row(201, HumanMessage(content="read a.py"), sequence=1),
            _row(202, AIMessage(content=""), sequence=2, metadata=_metadata([tool_call])),
            tool_row,
        ],
    )

    assert state["runs"][0]["messages"][1]["parts"] == [
        {
            **tool_call,
            "status": "completed",
            "error": None,
            "isError": False,
            "display_data": {"kind": "file", "nested": {"items": ["a"]}},
        }
    ]

    display_data = state["runs"][0]["messages"][1]["parts"][0]["display_data"]
    assert display_data is not result["display_data"]
    assert display_data is not None
    display_data["nested"]["items"].append("snapshot-only")
    assert result["display_data"] == {"kind": "file", "nested": {"items": ["a"]}}


@pytest.mark.parametrize(
    ("run_status", "expected_status", "expected_error", "expected_is_error"),
    [
        ("cancelled", "cancelled", "已取消", False),
        ("completed", "failed", "执行异常", True),
        ("failed", "failed", "执行异常", True),
    ],
)
def test_rebuild_settles_unmatched_terminal_tool_calls(
    run_status: str,
    expected_status: str,
    expected_error: str,
    expected_is_error: bool,
) -> None:
    tool_call = {
        "type": "tool-call",
        "toolCallId": "call-unmatched",
        "toolName": "read_file",
        "status": "running",
        "args": {},
        "presentation": {},
        "isError": False,
    }

    state = ConversationTaskStateRebuilder.rebuild(
        _task(),
        [_run(status=run_status, end_reason="terminal")],
        [_row(202, AIMessage(content=""), metadata=_metadata([tool_call]))],
    )

    assert state["runs"][0]["messages"][0]["parts"][0] == {
        **tool_call,
        "status": expected_status,
        "error": expected_error,
        "isError": expected_is_error,
    }


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_error", "expected_is_error"),
    [
        (
            {
                "status": "success",
                "display_data": {"kind": "file"},
                "status_hint": None,
                "error": None,
            },
            "completed",
            None,
            False,
        ),
        (
            {
                "status": "error",
                "display_data": {"kind": "tool-error", "status_hint": "文件不存在"},
                "status_hint": "文件不存在",
                "error": "full provider error must stay out of UI",
                "errorCode": "not_found",
                "isError": True,
            },
            "failed",
            "文件不存在",
            True,
        ),
        (
            {
                "status": "cancelled",
                "display_data": None,
                "status_hint": "用户取消",
                "error": "full cancellation diagnostic",
            },
            "cancelled",
            "已取消",
            False,
        ),
    ],
)
def test_rebuild_projects_tool_result_to_controlled_ui_error(
    result: dict[str, object],
    expected_status: str,
    expected_error: str | None,
    expected_is_error: bool,
) -> None:
    tool_call = {
        "type": "tool-call",
        "toolCallId": "call-result",
        "toolName": "read_file",
        "status": "running",
        "args": {},
        "presentation": {},
        "isError": False,
    }
    state = ConversationTaskStateRebuilder.rebuild(
        _task(),
        [_run()],
        [
            _row(202, AIMessage(content=""), metadata=_metadata([tool_call]), sequence=1),
            _row(
                203,
                ToolMessage(content="ignored", tool_call_id="call-result"),
                metadata=_metadata([], tool_result=result),
                sequence=2,
            ),
        ],
    )

    part = state["runs"][0]["messages"][0]["parts"][0]
    assert part["status"] == expected_status
    assert part["error"] == expected_error
    assert part["isError"] is expected_is_error
    if result.get("errorCode") is not None:
        assert part["errorCode"] == "not_found"


@pytest.mark.parametrize(
    "message,metadata",
    [
        (
            AIMessage(content=""),
            _metadata(
                [],
                tool_result={
                    "status": "success",
                    "display_data": None,
                    "status_hint": None,
                    "error": None,
                },
            ),
        ),
        (
            ToolMessage(content="tool", tool_call_id="call-1"),
            _metadata([{"type": "text", "text": "wrong"}]),
        ),
        (
            ToolMessage(content="tool", tool_call_id="call-1"),
            _metadata([]),
        ),
        (
            HumanMessage(content="human"),
            _metadata(
                [
                    {
                        "type": "tool-call",
                        "toolCallId": "call-1",
                        "toolName": "read_file",
                        "status": "pending",
                        "args": {},
                        "presentation": {},
                        "isError": False,
                    }
                ]
            ),
        ),
        (
            SystemMessage(content="system"),
            _metadata(
                [
                    {
                        "type": "tool-call",
                        "toolCallId": "call-1",
                        "toolName": "read_file",
                        "status": "pending",
                        "args": {},
                        "presentation": {},
                        "isError": False,
                    }
                ]
            ),
        ),
    ],
)
def test_rebuild_rejects_semantically_misplaced_transport_metadata(
    message: object, metadata: dict[str, object]
) -> None:
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(),
            [_run()],
            [
                _row(
                    204,
                    message,
                    run_id=None if isinstance(message, SystemMessage) else 11,
                    metadata=metadata,
                )
            ],
        )

    assert exc_info.value.code == "misplaced_transport_metadata"
    assert exc_info.value.context_row_id == 204


def test_rebuild_maps_task_usage_current_run_and_stably_sorts_runs() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cache_hit_tokens": 2,
        "cache_miss_tokens": 8,
        "reasoning_tokens": 1,
    }
    task = _task()
    task.current_run_id = 12
    task.context_usage_used = 123
    task.context_window_total = 1000
    state = ConversationTaskStateRebuilder.rebuild(
        task,
        [
            _run(12, created_at=datetime(2026, 9, 12, 0, 0, 2, tzinfo=UTC), usage=usage),
            _run(11, created_at=datetime(2026, 9, 12, 0, 0, 1, tzinfo=UTC)),
        ],
        [],
    )

    assert [run["runId"] for run in state["runs"]] == [11, 12]
    assert state["current_run_id"] == 12
    assert state["context_usage_used"] == 123
    assert state["context_window_total"] == 1000
    assert state["context_usage_ratio"] == 0.123
    assert state["runs"][1]["usage"] == usage
    assert state["error"] is None
    assert state["approvals"] == {}


@pytest.mark.parametrize(
    ("metadata", "message_schema_version", "code"),
    [
        (_metadata([], schema_version=2), 1, "unsupported_context_schema"),
        (_metadata([{"type": "unknown", "text": "bad"}]), 1, "malformed_context_metadata"),
        (_metadata([]), 2, "unsupported_context_schema"),
    ],
)
def test_rebuild_rejects_malformed_or_unsupported_context_metadata(
    metadata: dict[str, object], message_schema_version: int, code: str
) -> None:
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(),
            [_run()],
            [
                _row(
                    201,
                    AIMessage(content=""),
                    metadata=metadata,
                    message_schema_version=message_schema_version,
                )
            ],
        )

    assert exc_info.value.code == code
    assert exc_info.value.context_row_id == 201


def test_rebuild_rejects_context_row_from_another_task() -> None:
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(7), [_run(task_id=7)], [_row(201, HumanMessage(content="wrong"), task_id=8)]
        )

    assert exc_info.value.code == "context_task_mismatch"


def test_rebuild_rejects_context_row_for_an_orphan_run() -> None:
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(),
            [_run()],
            [_row(201, HumanMessage(content="orphan"), run_id=99)],
        )

    assert exc_info.value.code == "orphan_context_run"


def test_rebuild_rejects_tool_message_without_matching_ai_tool_call() -> None:
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(),
            [_run()],
            [
                _row(201, ToolMessage(content="orphan", tool_call_id="missing"), metadata=_metadata(
                    [],
                    tool_result={
                        "status": "success",
                        "display_data": None,
                        "status_hint": None,
                        "error": None,
                    },
                ))
            ],
        )

    assert exc_info.value.code == "orphan_tool_message"


def test_rebuild_rejects_duplicate_tool_call_id() -> None:
    part = {
        "type": "tool-call",
        "toolCallId": "duplicate",
        "toolName": "read_file",
        "status": "pending",
        "args": {},
        "presentation": {},
        "isError": False,
    }
    with pytest.raises(ConversationStateRebuildError) as exc_info:
        ConversationTaskStateRebuilder.rebuild(
            _task(),
            [_run()],
            [_row(201, AIMessage(content=""), metadata=_metadata([part, dict(part)]))],
        )

    assert exc_info.value.code == "duplicate_tool_call_id"


def test_agent_context_loader_returns_only_included_rows_in_sequence_and_includes_system() -> None:
    rows = [
        _row(3, SystemMessage(content="system"), run_id=None, sequence=3),
        _row(1, HumanMessage(content="hidden"), sequence=1, include_in_context=False),
        _row(2, HumanMessage(content="visible"), sequence=2),
    ]

    messages = AgentContextLoader.load(7, rows)

    assert [type(message) for message in messages] == [HumanMessage, SystemMessage]
    assert [message.content for message in messages] == ["visible", "system"]


def test_validate_snapshot_rejects_current_run_that_is_not_present() -> None:
    invalid_state = {
        "runs": [],
        "current_run_id": 99,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }

    with pytest.raises(ValueError, match="current_run_id must refer to a run"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]
