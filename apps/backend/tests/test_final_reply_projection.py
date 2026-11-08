"""A9：最终回复来自响应事实；实时多段文本与冷重建的结论一致。"""

from datetime import UTC, datetime

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.service.conversation_task_state_service import _apply_mutation
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
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.message_metadata import AssistantMessageMetadata
from app.models.task_record import TaskRecord

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def apply(state, event):
    for mutation in plan_event(event, state):
        _apply_mutation(state, mutation)


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
    apply(state, RunInitializedEvent(task_id=7, run_id=1, created_at=NOW))
    for sequence, text in [(1, "中途进展"), (2, content)]:
        apply(
            state,
            AssistantTextDeltaEvent(task_id=7, run_id=1, source_sequence=sequence, delta=text),
        )
        apply(state, AssistantPartClosedEvent(task_id=7, run_id=1, source_sequence=sequence))
    apply(
        state,
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=1,
            status=ConversationRunStatus(status),
            end_reason=reason,
            response_sequence=2,
        ),
    )
    validate_snapshot(state)
    rows = [
        ConversationTaskContextRecord(
            task_id=7,
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
    task = TaskRecord(
        id=7, workspace_id=1, title="验收", current_run_id=1, created_at=NOW, updated_at=NOW
    )
    run = ConversationRunRecord(
        id=1,
        task_id=7,
        status=status,
        end_reason=reason,
        final_output="结构化支线的其他内容",
        created_at=NOW,
        updated_at=NOW,
    )
    cold = ConversationTaskStateRebuilder.rebuild(task, [run], rows)
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
    apply(state, RunInitializedEvent(task_id=7, run_id=1, created_at=NOW))
    for sequence, text in [(1, "草稿"), (2, "结论上半"), (2, "结论下半")]:
        apply(
            state,
            AssistantTextDeltaEvent(task_id=7, run_id=1, source_sequence=sequence, delta=text),
        )
        apply(state, AssistantPartClosedEvent(task_id=7, run_id=1, source_sequence=sequence))
    apply(
        state,
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
            end_reason="stop",
            response_sequence=2,
        ),
    )
    apply(state, RunResultUpdatedEvent(task_id=7, run_id=1, end_reason="structured_output_failed"))
    assert [bool(p.get("finalReply")) for p in state["messages"][0]["parts"]] == [False, True, True]
    validate_snapshot(state)
