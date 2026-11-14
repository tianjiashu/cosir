"""A9：最终回复来自响应事实；实时多段文本与冷重建的结论一致。"""

from datetime import UTC, datetime

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.snapshot_rebuild import (
    rebuild_snapshot,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
)
from app.models.enums.run_status import RunStatus
from app.models.message_metadata import AssistantMessageMetadata
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.session_record import SessionRecord

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def apply(state, event):
    for mutation in plan_event(event, state, {}):
        mutation.apply_to(state)


@pytest.mark.parametrize(
    "status,reason,content,expected",
    [
        ("completed", "stop", "最终结论", True),
        ("completed", "length", "被截断", False),
        ("completed", "stop", "  ", False),
        ("cancelled", "user_cancelled", "部分内容", False),
        ("cancelled", "runtime_restarted", "中断内容", False),
        ("failed", "max_steps", "诊断信息", False),
    ],
)
def test_live_and_rebuild_agree(status, reason, content, expected):
    state = empty_snapshot()
    apply(state, RunInitializedEvent(session_id=7, run_id=1, created_at=NOW))
    for sequence, text in [(1, "中途进展"), (2, content)]:
        apply(
            state,
            AssistantTextDeltaEvent(session_id=7, run_id=1, turn=1, source_sequence=sequence, delta=text),
        )
        apply(state, AssistantPartClosedEvent(session_id=7, run_id=1, turn=1, source_sequence=sequence))
    apply(
        state,
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            session_id=7,
            run_id=1,
            status=RunStatus(status),
            end_reason=reason,
            response_sequence=2,
        ),
    )
    validate_snapshot(state)
    rows = [
        SessionContextRecord(
            session_id=7,
            run_id=1,
            sequence=sequence,
            include_in_context=True,
            message={"role": "assistant", "content": text},
            metadata=AssistantMessageMetadata(
                finish_reason=reason
            ),
            turn=1,
        )
        for sequence, text in [(1, "中途进展"), (2, content)]
    ]
    session = SessionRecord(
        id=7, workspace_id=1, title="验收", current_run_id=1, created_at=NOW, updated_at=NOW
    )
    run = RunRecord(
        id=1,
        session_id=7,
        status=status,
        end_reason=reason,
        final_output="结构化支线的其他内容",
        created_at=NOW,
        updated_at=NOW,
    )
    cold = rebuild_snapshot(session, [run], rows, {})
    validate_snapshot(cold)
    for snapshot in (state, cold):
        marked = [
            part["text"]
            for message in snapshot["messages"]
            for part in message["parts"]
            if part.get("finalReply")
        ]
        assert marked == ([content] if expected else [])


def test_all_text_parts_from_final_response_marked_and_structured_result_does_not_erase():
    state = empty_snapshot()
    apply(state, RunInitializedEvent(session_id=7, run_id=1, created_at=NOW))
    for sequence, text in [(1, "草稿"), (2, "结论上半"), (2, "结论下半")]:
        apply(
            state,
            AssistantTextDeltaEvent(session_id=7, run_id=1, turn=1, source_sequence=sequence, delta=text),
        )
        apply(state, AssistantPartClosedEvent(session_id=7, run_id=1, turn=1, source_sequence=sequence))
    apply(
        state,
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            session_id=7,
            run_id=1,
            status=RunStatus.COMPLETED,
            end_reason="stop",
            response_sequence=2,
        ),
    )
    apply(state, RunResultUpdatedEvent(session_id=7, run_id=1, end_reason="structured_output_failed"))
    assert [bool(p.get("finalReply")) for p in state["messages"][0]["parts"]] == [False, True, True]
    validate_snapshot(state)


def _complete_turn(state, run_id: int, turn: int, text: str, end_reason: str = "stop") -> None:
    apply(state, RunInitializedEvent(session_id=7, run_id=run_id, created_at=NOW))
    apply(
        state,
        AssistantTextDeltaEvent(session_id=7, run_id=run_id, turn=turn, source_sequence=turn, delta=text),
    )
    apply(state, AssistantPartClosedEvent(session_id=7, run_id=run_id, turn=turn, source_sequence=turn))
    apply(
        state,
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, turn, tzinfo=UTC),
            session_id=7,
            run_id=run_id,
            status=RunStatus.COMPLETED,
            end_reason=end_reason,
            response_sequence=turn,
        ),
    )


def test_later_runs_neither_clear_earlier_final_replies_nor_receive_their_results():
    """后一个 Run 的最终回复与结构化结果只落在它自己的回复上，不清除、不改写前一个 Run 的。"""
    state = empty_snapshot()
    _complete_turn(state, 1, 1, "第一个结论")
    _complete_turn(state, 2, 2, "第二个结论")
    apply(state, RunResultUpdatedEvent(session_id=7, run_id=2, end_reason="structured_output_failed"))
    validate_snapshot(state)
    first, second = state["messages"]
    assert (first["id"], second["id"]) == ("assistant-turn-1", "assistant-turn-2")
    assert [p.get("finalReply") for p in first["parts"]] == [True]
    assert [p.get("finalReply") for p in second["parts"]] == [True]
    assert first["outcome"]["endReason"] == "stop"
    assert second["outcome"]["endReason"] == "structured_output_failed"
