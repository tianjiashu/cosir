"""Rebuild-boundary tests for the canonical Transport snapshot assembler."""

import json
from datetime import UTC, datetime

import pytest

from app.assistant_transport.projection.snapshot_rebuild import (
    pair_tool_parts,
    rebuild_snapshot,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.conversation_input import UserInputPresentation
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
from app.models.session_record import SessionRecord


def _timestamp() -> datetime:
    return datetime(2026, 9, 12, tzinfo=UTC)


def _session(session_id: int = 7) -> SessionRecord:
    timestamp = _timestamp()
    return SessionRecord(
        id=session_id,
        workspace_id=3,
        title="session",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_rebuild_empty_session_returns_valid_empty_snapshot() -> None:
    state = rebuild_snapshot(_session(), [], [], {})

    assert state == {
        "title": "session",
        "messages": [],
        "messageOffset": 0,
        "activity": None,
        "terminal_sessions": [],
        "background_tasks": [],
        "foreground_waits": [],
        "handoffs": [],
            "queued_messages": [],
    }


@pytest.mark.parametrize("window", [None, 64_000])
def test_rebuild_activity_uses_latest_runs_frozen_context_window(window: int | None) -> None:
    """重建只消费最新 Run 已冻结的窗口，历史 Run 的窗口不进 wire。"""
    timestamp = _timestamp()
    session = _session()
    session.current_run_id = 2
    runs = [
        RunRecord(
            id=index,
            session_id=session.id,
            status="completed",
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
            context_window_total=frozen,
        )
        for index, frozen in ((1, 128_000), (2, window))
    ]
    state = rebuild_snapshot(session, runs, [], {})
    validate_snapshot(state)
    assert state["activity"] is not None
    # 当前 Run 还没写过行：没有可见回合，completed 也不可恢复。
    assert (state["activity"]["turn"], state["activity"]["resumable"]) == (None, False)
    assert state["activity"]["contextWindowTotal"] == window


def test_rebuild_session_without_current_run_has_no_activity() -> None:
    state = rebuild_snapshot(_session(), [], [], {})

    assert state["activity"] is None


def test_rebuild_writes_outcome_only_on_terminal_runs_last_assistant_message() -> None:
    """冷重建：终态 Run 的结局写在其最后一条助手消息上，运行中的 Run 不写结局。"""
    timestamp = _timestamp()
    session = _session()
    session.current_run_id = 2
    runs = [
        RunRecord(
            id=1,
            session_id=session.id,
            status="completed",
            end_reason="final_answer",
            context_window_total=64_000,
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
        ),
        RunRecord(
            id=2,
            session_id=session.id,
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
            execution_data=ReactExecutionState(),
        ),
    ]
    rows = [
        SessionContextRecord(
            id=sequence,
            session_id=session.id,
            run_id=run_id,
            message={"role": "assistant", "content": text},
            include_in_context=True,
            sequence=sequence,
            turn=turn,
        )
        for sequence, run_id, turn, text in (
            (1, 1, 1, "第一段"),
            (2, 1, 1, "第二段"),
            (3, 2, 2, "进行中"),
        )
    ]

    state = rebuild_snapshot(session, runs, rows, {})

    validate_snapshot(state)
    run_one = [m for m in state["messages"] if m["turn"] == 1]
    run_two = [m for m in state["messages"] if m["turn"] == 2]
    assert run_one[-1]["outcome"] is not None
    assert run_one[-1]["outcome"]["status"] == "completed"
    assert run_one[-1]["outcome"]["endReason"] == "final_answer"
    # 冷重建同样把该 Run 冻结的窗口写进结局，与它的用量同源。
    assert run_one[-1]["outcome"]["contextWindowTotal"] == 64_000
    assert all(m["outcome"] is None for m in run_one[:-1])
    assert all(m["outcome"] is None for m in run_two)
    assert state["activity"] is not None
    assert state["activity"]["status"] == "running"
    assert state["activity"]["turn"] == 2


def test_validate_snapshot_rejects_outcome_of_non_terminal_status() -> None:
    invalid_state = {
        "title": "session",
        "messages": [
            {
                "id": "assistant-turn-1",
                "role": "assistant",
                "parts": [],
                "turn": 1,
                "inherited": False,
                "outcome": {
                    "status": "running",
                    "endReason": None,
                    "error": None,
                    "usage": None,
                    "contextWindowTotal": None,
                    "durationMs": 0,
                },
                "source": None,
            }
        ],
        "messageOffset": 0,
        "activity": None,
        "terminal_sessions": [],
        "background_tasks": [],
        "foreground_waits": [],
        "handoffs": [],
        "queued_messages": [],
    }

    with pytest.raises(ValueError, match="outcome status must be terminal"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]


def test_validate_snapshot_rejects_outcome_on_user_message() -> None:
    invalid_state = {
        "title": "session",
        "messages": [
            {
                "id": "1",
                "role": "user",
                "parts": [],
                "turn": 1,
                "inherited": False,
                "outcome": {
                    "status": "completed",
                    "endReason": None,
                    "error": None,
                    "usage": None,
                    "contextWindowTotal": None,
                    "durationMs": 0,
                },
                "source": None,
            }
        ],
        "messageOffset": 0,
        "activity": None,
        "terminal_sessions": [],
        "background_tasks": [],
        "foreground_waits": [],
        "handoffs": [],
        "queued_messages": [],
    }

    with pytest.raises(ValueError, match="only assistant messages carry an outcome"):
        validate_snapshot(invalid_state)  # type: ignore[arg-type]


def test_rebuild_keeps_interrupted_output_stopped_even_when_run_is_active() -> None:
    """已落库的中断输出已停止生成，后续 Run 继续执行也不能把旧文本恢复为 running。"""

    timestamp = _timestamp()
    run = RunRecord(
        id=1,
        session_id=7,
        status="running",
        end_reason=None,
        created_at=timestamp,
        updated_at=timestamp,
        execution_data=ReactExecutionState(),
    )
    row = SessionContextRecord(
        id=1,
        session_id=7,
        run_id=1,
        message={"role": "assistant", "content": "输出到一半"},
        include_in_context=False,
        sequence=1,
        turn=1,
    )

    state = rebuild_snapshot(_session(), [run], [row], {})

    assert state["messages"][0]["parts"] == [
        {
            "type": "text",
            "text": "输出到一半",
            "status": "completed",
            "sourceSequence": 1,
            "finalReply": False,
        }
    ]
    validate_snapshot(state)


def test_rebuild_keeps_interrupted_output_when_run_is_cancelled() -> None:
    """重启恢复会先取消遗留 Run，partial 不应在终态快照中继续显示 running。"""

    timestamp = _timestamp()
    run = RunRecord(
        id=1,
        session_id=7,
        status="cancelled",
        end_reason="backend_restarted",
        created_at=timestamp,
        updated_at=timestamp,
        execution_data=ReactExecutionState(),
    )
    row = SessionContextRecord(
        id=1,
        session_id=7,
        run_id=1,
        message={"role": "assistant", "content": "输出到一半"},
        include_in_context=False,
        sequence=1,
        turn=1,
    )

    state = rebuild_snapshot(_session(), [run], [row], {})

    assert state["messages"][0]["parts"] == [
        {
            "type": "text",
            "text": "输出到一半",
            "status": "completed",
            "sourceSequence": 1,
            "finalReply": False,
        }
    ]


def test_rebuild_restores_child_session_locator_and_workspace_role_from_display_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SessionContextRecord(
        id=1,
        session_id=7,
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
        turn=1,
    )
    result_row = SessionContextRecord(
        id=2,
        session_id=7,
        run_id=1,
        message={"role": "tool", "content": "started", "tool_call_id": "delegate-1"},
        include_in_context=False,
        sequence=2,
        metadata=ToolMessageMetadata.model_validate(
            {
                "status": "completed",
                "display_data": {
                    "kind": "delegation-result",
                    "child_session_id": 22,
                    "child_agent_id": "reviewer",
                    "role": "workspace-reviewer",
                },
            }
        ),
        turn=1,
    )

    tool_part = pair_tool_parts(
        [row, result_row], {},
    )["delegate-1"]

    assert tool_part["child_session_id"] == 22
    assert tool_part["agent_role"] == "workspace-reviewer"
    assert tool_part["display_data"]["role"] == "workspace-reviewer"


def test_rebuild_restores_capsule_text_and_images_from_context_input() -> None:
    timestamp = _timestamp()
    run = RunRecord(
        id=1,
        session_id=7,
        status="completed",
        created_at=timestamp,
        updated_at=timestamp,
    )
    image_id = "a" * 64
    text = '请查看 <ref path="src/a.py" lines="1-2" />'
    presentation = UserInputPresentation(text=text, image_paths=[f"attachments/{image_id}.png"])
    row = SessionContextRecord(
        id=1,
        session_id=7,
        run_id=1,
        message={"role": "user", "content": text},
        metadata=UserMessageMetadata(kind="user_input", presentation=presentation),
        include_in_context=True,
        sequence=1,
        turn=1,
    )

    state = rebuild_snapshot(_session(), [run], [row], {})

    assert state["messages"][0]["parts"] == [
        {"type": "text", "text": text, "status": "completed"},
        {"type": "image", "image": f"cosir-attachment://{image_id}"},
    ]
    validate_snapshot(state)


def test_rebuild_does_not_invent_user_message_from_run() -> None:
    timestamp = _timestamp()
    run = RunRecord(
        id=1,
        session_id=7,
        status="running",
        created_at=timestamp,
        updated_at=timestamp,
    )

    state = rebuild_snapshot(_session(), [run], [], {})

    assert state["messages"] == []
    validate_snapshot(state)


def test_legacy_input_wrapper_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        UserInputPresentation.model_validate({"assistant_input": {}})


def test_reasoning_stays_in_context_observation_but_not_cold_conversation() -> None:
    from app.api.schemas.response.ContextMessageResponse import ContextMessageResponse

    timestamp = _timestamp()
    run = RunRecord(
        id=1, session_id=7, status="completed", created_at=timestamp, updated_at=timestamp
    )
    row = SessionContextRecord(
        session_id=7,
        run_id=1,
        sequence=1,
        include_in_context=True,
        message={"role": "assistant", "content": "结论", "reasoning_content": "推理原文"},
        created_at=timestamp.isoformat(),
        updated_at=timestamp.isoformat(),
        turn=1,
    )
    state = rebuild_snapshot(_session(), [run], [row], {})
    validate_snapshot(state)
    assert state["messages"][0]["parts"] == [
        {
            "type": "text",
            "text": "结论",
            "status": "completed",
            "sourceSequence": 1,
            "finalReply": False,
        }
    ]
    assert "推理原文" not in json.dumps(state, ensure_ascii=False)
    assert ContextMessageResponse.from_record(row).reasoning_content == "推理原文"
    assert row.message["reasoning_content"] == "推理原文"


@pytest.mark.usefixtures("backend_env")
@pytest.mark.parametrize(
    "name, expected",
    [
        ("wait_messages", "quiet"),
        ("delegate_task", "standalone"),
        ("fork_session", "standalone"),
        ("read_file", "activity"),
        ("removed_tool", "activity"),
    ],
)
def test_live_and_rebuilt_calls_share_definition_presentation(name, expected):
    from app.assistant_transport.projection.tool_call_event_projection import plan_tool_call_created
    from app.assistant_transport.projection.tool_presentation import presentation_catalog
    from app.config.configuration import get_tool_registry
    from app.core.events import ToolCallCreatedEvent

    registry = get_tool_registry()
    catalog = presentation_catalog(registry)
    session = _session()
    session.current_run_id = 1
    run = RunRecord(
        id=1, session_id=7, status="running", created_at=_timestamp(), updated_at=_timestamp()
    )
    state = rebuild_snapshot(session, [run], [], catalog)
    mutations = plan_tool_call_created(
        ToolCallCreatedEvent(session_id=7, run_id=1, turn=1, tool_call_id="call", tool_name=name),
        state,
        catalog,
    )
    created = next(m for m in mutations if m.path[0] == "messages")
    assert created.value["parts"][0]["presentation"] == expected
    row = SessionContextRecord(
        session_id=7,
        run_id=1,
        sequence=1,
        include_in_context=True,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        turn=1,
    )
    rebuilt = rebuild_snapshot(session, [run], [row], catalog)
    validate_snapshot(rebuilt)
    assert rebuilt["messages"][0]["parts"][0]["presentation"] == expected
    if name != "removed_tool":
        schema = registry.get_tool_definition(name).to_model_tool_definition()
        assert set(schema) == {"name", "description", "strict", "parameters"}
