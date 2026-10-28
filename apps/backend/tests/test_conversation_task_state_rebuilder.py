"""Rebuild-boundary tests for the canonical Transport snapshot assembler."""

import json
from datetime import UTC, datetime

import pytest

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.models.conversation_input import UserInputPresentation
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
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


def test_rebuild_empty_task_returns_valid_empty_snapshot() -> None:
    state = ConversationTaskStateRebuilder.rebuild(_task(), [], [])

    assert state == {
        "title": "task",
        "messages": [],
        "activity": None,
        "terminal_sessions": [],
        "deferred_results": [],
    }


@pytest.mark.parametrize("window", [None, 64_000])
def test_rebuild_activity_uses_latest_runs_frozen_context_window(window: int | None) -> None:
    """重建只消费最新 Run 已冻结的窗口，历史 Run 的窗口不进 wire。"""
    timestamp = _timestamp()
    task = _task()
    task.current_run_id = 2
    runs = [
        ConversationRunRecord(
            id=index,
            task_id=task.id,
            status="completed",
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
            context_window_total=frozen,
        )
        for index, frozen in ((1, 128_000), (2, window))
    ]
    state = ConversationTaskStateRebuilder.rebuild(task, runs, [])
    validate_snapshot(state)
    assert state["activity"] is not None
    assert state["activity"]["runId"] == 2
    assert state["activity"]["contextWindowTotal"] == window


def test_rebuild_task_without_current_run_has_no_activity() -> None:
    state = ConversationTaskStateRebuilder.rebuild(_task(), [], [])

    assert state["activity"] is None


def test_rebuild_writes_outcome_only_on_terminal_runs_last_assistant_message() -> None:
    """冷重建：终态 Run 的结局写在其最后一条助手消息上，运行中的 Run 不写结局。"""
    timestamp = _timestamp()
    task = _task()
    task.current_run_id = 2
    runs = [
        ConversationRunRecord(
            id=1,
            task_id=task.id,
            status="completed",
            end_reason="final_answer",
            context_window_total=64_000,
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
        ),
        ConversationRunRecord(
            id=2,
            task_id=task.id,
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
        ),
    ]
    rows = [
        ConversationTaskContextRecord(
            id=sequence,
            task_id=task.id,
            run_id=run_id,
            message={"role": "assistant", "content": text},
            include_in_context=True,
            sequence=sequence,
        )
        for sequence, run_id, text in ((1, 1, "第一段"), (2, 1, "第二段"), (3, 2, "进行中"))
    ]

    state = ConversationTaskStateRebuilder.rebuild(task, runs, rows)

    validate_snapshot(state)
    run_one = [m for m in state["messages"] if m["runId"] == 1]
    run_two = [m for m in state["messages"] if m["runId"] == 2]
    assert run_one[-1]["outcome"] is not None
    assert run_one[-1]["outcome"]["status"] == "completed"
    assert run_one[-1]["outcome"]["endReason"] == "final_answer"
    # 冷重建同样把该 Run 冻结的窗口写进结局，与它的用量同源。
    assert run_one[-1]["outcome"]["contextWindowTotal"] == 64_000
    assert all(m["outcome"] is None for m in run_one[:-1])
    assert all(m["outcome"] is None for m in run_two)
    assert state["activity"] is not None
    assert state["activity"]["status"] == "running"


def test_validate_snapshot_rejects_outcome_of_non_terminal_status() -> None:
    invalid_state = {
        "title": "task",
        "messages": [
            {
                "id": "assistant-run-1",
                "role": "assistant",
                "parts": [],
                "runId": 1,
                "outcome": {"status": "running", "endReason": None, "error": None, "usage": None, "contextWindowTotal": None},
            }
        ],
        "activity": None,
        "terminal_sessions": [],
        "deferred_results": [],
    }

    with pytest.raises(ValueError, match="outcome status must be terminal"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]


def test_validate_snapshot_rejects_outcome_on_user_message() -> None:
    invalid_state = {
        "title": "task",
        "messages": [
            {
                "id": "1",
                "role": "user",
                "parts": [],
                "runId": 1,
                "outcome": {"status": "completed", "endReason": None, "error": None, "usage": None, "contextWindowTotal": None},
            }
        ],
        "activity": None,
        "terminal_sessions": [],
        "deferred_results": [],
    }

    with pytest.raises(ValueError, match="only assistant messages carry an outcome"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]


def test_get_tool_display_returns_empty_dict_for_missing_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 未注册工具 / 无 display 时返回 {} 而非 None，保证重建快照的 presentation
    # 是合法对象、能通过 validate_snapshot（修复前返回 None 会让整张快照冷读崩）。

    class _FakeRegistry:
        def get_tool_definition(self, _name: str):
            return None

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRegistry(),
    )
    assert ConversationTaskStateRebuilder.get_tool_display("unknown_tool") == {}


def test_validate_snapshot_tolerates_none_presentation() -> None:
    # 防御性：presentation 为显式 None 视作缺省，不再使整张快照校验崩溃
    # （旧实现 ``isinstance(part.get("presentation", {}), dict)`` 会命中 None）。
    state = {
        "title": "task",
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool-call",
                        "toolCallId": "c1",
                        "toolName": "read_file",
                        "status": "cancelled",
                        "presentation": None,
                    }
                ],
                "runId": 1,
                "outcome": None,
            }
        ],
        "activity": None,
        "terminal_sessions": [],
        "deferred_results": [],
    }

    validate_snapshot(state)  # 不应抛 ValueError


def test_rebuild_keeps_interrupted_output_stopped_even_when_run_is_active() -> None:
    """已落库的中断输出已停止生成，后续 Run 继续执行也不能把旧文本恢复为 running。"""

    timestamp = _timestamp()
    run = ConversationRunRecord(
        id=1,
        task_id=7,
        status="running",
        end_reason=None,
        created_at=timestamp,
        updated_at=timestamp,
        execution_data=ReactExecutionState(),
    )
    row = ConversationTaskContextRecord(
        id=1,
        task_id=7,
        run_id=1,
        message={"role": "assistant", "content": "输出到一半"},
        include_in_context=False,
        sequence=1,
    )

    state = ConversationTaskStateRebuilder.rebuild(_task(), [run], [row])

    assert state["messages"][0]["parts"] == [
        {"type": "text", "text": "输出到一半", "status": "completed"}
    ]
    validate_snapshot(state)


def test_rebuild_keeps_interrupted_output_when_run_is_cancelled() -> None:
    """重启恢复会先取消遗留 Run，partial 不应在终态快照中继续显示 running。"""

    timestamp = _timestamp()
    run = ConversationRunRecord(
        id=1,
        task_id=7,
        status="cancelled",
        end_reason="backend_restarted",
        created_at=timestamp,
        updated_at=timestamp,
        execution_data=ReactExecutionState(),
    )
    row = ConversationTaskContextRecord(
        id=1,
        task_id=7,
        run_id=1,
        message={"role": "assistant", "content": "输出到一半"},
        include_in_context=False,
        sequence=1,
    )

    state = ConversationTaskStateRebuilder.rebuild(_task(), [run], [row])

    assert state["messages"][0]["parts"] == [
        {"type": "text", "text": "输出到一半", "status": "completed"}
    ]


def test_rebuild_restores_child_task_locator_and_workspace_role_from_display_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ToolRegistry:
        def get_tool_definition(self, _name: str):
            return None

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _ToolRegistry(),
    )
    row = ConversationTaskContextRecord(
        id=1,
        task_id=7,
        run_id=1,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "delegate-1",
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(
                            {
                                "child_agent_id": "reviewer",
                                "role": "workspace-reviewer",
                                "message": "review",
                                "agent_name": "审查代码",
                            }
                        ),
                    },
                }
            ],
        },
        include_in_context=False,
        sequence=1,
    )
    result_row = ConversationTaskContextRecord(
        id=2,
        task_id=7,
        run_id=1,
        message={"role": "tool", "content": "started", "tool_call_id": "delegate-1"},
        include_in_context=False,
        sequence=2,
        metadata=ToolMessageMetadata.model_validate(
            {
                "status": "completed",
                "display_data": {
                    "kind": "delegation-result",
                    "child_task_id": 22,
                    "child_agent_id": "reviewer",
                    "role": "workspace-reviewer",
                },
            }
        ),
    )

    tool_part = ConversationTaskStateRebuilder.build_pair_tool_part(
        [row, result_row],
    )["delegate-1"]

    assert tool_part["child_task_id"] == 22
    assert tool_part["agent_role"] == "workspace-reviewer"
    assert tool_part["display_data"]["role"] == "workspace-reviewer"


def test_rebuild_restores_ordinary_file_from_context_input() -> None:
    timestamp = _timestamp()
    run = ConversationRunRecord(
        id=1,
        task_id=7,
        status="completed",
        created_at=timestamp,
        updated_at=timestamp,
    )
    presentation = UserInputPresentation(
        display_text="请查看 [[cosir-file:file-1]]",
        attachments=[
            {
                "id": "file-1",
                "name": "notes.md",
                "content_type": "text/markdown",
                "path": "C:/workspace/notes.md",
            }
        ],
    )
    row = ConversationTaskContextRecord(
        id=1,
        task_id=7,
        run_id=1,
        message={"role": "user", "content": "请查看 C:/workspace/notes.md"},
        metadata=UserMessageMetadata(kind="user_input", presentation=presentation),
        include_in_context=True,
        sequence=1,
    )

    state = ConversationTaskStateRebuilder.rebuild(_task(), [run], [row])

    assert state["messages"][0]["parts"] == [
        {"type": "text", "text": "请查看 ", "status": "completed"},
        {
            "type": "file",
            "file": "cosir-local-file:file-1",
            "name": "notes.md",
            "contentType": "text/markdown",
        },
    ]
    validate_snapshot(state)


def test_rebuild_does_not_invent_user_message_from_run() -> None:
    timestamp = _timestamp()
    run = ConversationRunRecord(
        id=1,
        task_id=7,
        status="running",
        created_at=timestamp,
        updated_at=timestamp,
    )

    state = ConversationTaskStateRebuilder.rebuild(_task(), [run], [])

    assert state["messages"] == []
    validate_snapshot(state)


def test_legacy_input_wrapper_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        UserInputPresentation.model_validate({"assistant_input": {}})


def test_blank_file_path_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        UserInputPresentation.model_validate(
            {
                "display_text": "请查看 [[cosir-file:file-1]]",
                "attachments": [
                    {
                        "id": "file-1",
                        "name": "notes.md",
                        "content_type": "text/markdown",
                        "path": "   ",
                    }
                ],
                "input_text": "请查看 notes.md",
            }
        )
