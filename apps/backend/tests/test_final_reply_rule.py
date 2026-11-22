"""最终回复由哪几条回复组成：回复本身那类工具调用（按钮、交付卡）不把它前面的答案推进过程。

潜在缺陷：模型把答案和 ``offer_next_step`` 写在同一条回复里、调用之后只补一句收尾时，答案被当成过程折叠，
分叉 Agent 汇报给父 Agent 的 ``final_output`` 也只剩那句收尾。
"""

from datetime import UTC, datetime

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.snapshot_rebuild import rebuild_snapshot
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.core.context.final_reply import CommittedResponse, final_reply_sequences
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    RunInitializedEvent,
    RunStatusChangedEvent,
)
from app.models.message_context.message_metadata.assistant_message_metadata import (
    AssistantMessageMetadata,
)
from app.models.message_context.session_context import SessionContextRecord
from app.models.run.run_record import RunRecord
from app.models.run.run_status import RunStatus
from app.models.session.session_record import SessionRecord

NOW = datetime(2026, 11, 18, tzinfo=UTC)
REPLY = frozenset({"offer_next_step", "present_deliverables"})


def response(sequence: int, *tools: str, turn: int = 1) -> CommittedResponse:
    return CommittedResponse(sequence, turn, tools)


def test_answer_written_with_a_reply_tool_belongs_to_the_final_reply() -> None:
    # 答案与按钮写在同一条回复里，之后补一句收尾：两条都是最终回复。
    assert final_reply_sequences(
        [response(1, "browser_navigate"), response(3, "offer_next_step"), response(5)], REPLY
    ) == [3, 5]
    # 交付卡与按钮连着调用，也都算回复本身。
    assert final_reply_sequences(
        [response(3, "present_deliverables"), response(5, "offer_next_step"), response(7)], REPLY
    ) == [3, 5, 7]
    # 答案写在调用之后：只有最后一条（调用那条正文为空，不影响）。
    assert final_reply_sequences([response(3, "offer_next_step"), response(5)], REPLY) == [3, 5]


def test_process_calls_and_turn_boundaries_stop_the_final_reply() -> None:
    # 普通工具调用之前的正文仍是过程；混着普通调用的回复也不算回复本身。
    assert final_reply_sequences([response(1, "read_file"), response(3)], REPLY) == [3]
    assert final_reply_sequences(
        [response(1, "offer_next_step", "read_file"), response(3)], REPLY
    ) == [3]
    # 跨回合不连：上一个回合的按钮不属于这一回合的回复。
    assert final_reply_sequences(
        [response(1, "offer_next_step", turn=1), response(3, turn=2)], REPLY
    ) == [3]
    # Run 没在回复上结束（最后一条还带调用）：没有最终回复。
    assert final_reply_sequences([response(1), response(3, "offer_next_step")], REPLY) == []
    assert final_reply_sequences([], REPLY) == []


def _rows() -> list[SessionContextRecord]:
    def row(sequence: int, text: str, tool: str | None, finish: str) -> SessionContextRecord:
        message: dict = {"role": "assistant", "content": text}
        if tool:
            message["tool_calls"] = [
                {
                    "id": f"call-{sequence}",
                    "type": "function",
                    "function": {"name": tool, "arguments": "{}"},
                }
            ]
        return SessionContextRecord(
            session_id=7,
            run_id=1,
            sequence=sequence,
            include_in_context=True,
            message=message,
            metadata=AssistantMessageMetadata(finish_reason=finish),
            turn=1,
        )

    return [
        row(1, "", "browser_navigate", "tool_calls"),
        row(3, "四篇高赞帖的完整答案", "offer_next_step", "tool_calls"),
        row(5, "想细读告诉我", None, "stop"),
    ]


def _finals(snapshot) -> list[str]:
    return [
        part["text"]
        for message in snapshot["messages"]
        for part in message["parts"]
        if part.get("finalReply")
    ]


def test_live_projection_and_cold_rebuild_mark_the_same_answer() -> None:
    session = SessionRecord(
        id=7, workspace_id=1, title="验收", current_run_id=1, created_at=NOW, updated_at=NOW
    )
    run = RunRecord(
        id=1, session_id=7, status="completed", end_reason="stop", created_at=NOW, updated_at=NOW
    )
    cold = rebuild_snapshot(session, [run], _rows(), {}, reply_tools=REPLY)
    validate_snapshot(cold)
    assert _finals(cold) == ["四篇高赞帖的完整答案", "想细读告诉我"]

    state = empty_snapshot()
    for mutation_event in [RunInitializedEvent(session_id=7, run_id=1, created_at=NOW)]:
        for mutation in plan_event(mutation_event, state, {}):
            mutation.apply_to(state)
    for sequence, text in [(3, "四篇高赞帖的完整答案"), (5, "想细读告诉我")]:
        for event in [
            AssistantTextDeltaEvent(
                session_id=7, run_id=1, turn=1, source_sequence=sequence, delta=text
            ),
            AssistantPartClosedEvent(session_id=7, run_id=1, turn=1, source_sequence=sequence),
        ]:
            for mutation in plan_event(event, state, {}):
                mutation.apply_to(state)
    done = RunStatusChangedEvent(
        changed_at=NOW,
        session_id=7,
        run_id=1,
        status=RunStatus.COMPLETED,
        end_reason="stop",
        response_sequences=[3, 5],
    )
    for mutation in plan_event(done, state, {}):
        mutation.apply_to(state)
    assert _finals(state) == _finals(cold)

    # 不认识回复本身那类工具时（旧规则）只剩收尾——这正是被修掉的现象。
    assert _finals(rebuild_snapshot(session, [run], _rows(), {})) == ["想细读告诉我"]


# 目的：分叉 Agent 汇报给父 Agent 的 ``final_output`` 带上完整答案；潜在缺陷：只剩调用之后那句收尾。
def test_final_output_joins_every_final_reply_segment() -> None:
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    def entry(
        sequence: int, content: str, tool: str | None = None, run_id: int = 9
    ) -> ContextEntry:
        message: dict = {"role": "assistant", "content": content}
        if tool:
            message["tool_calls"] = [
                {
                    "id": f"c{sequence}",
                    "type": "function",
                    "function": {"name": tool, "arguments": "{}"},
                }
            ]
        return ContextEntry(message, run_id, sequence, turn=1)

    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_run_id = 9
    manager._entries = [
        entry(1, "上一个 Run 的话", run_id=8),
        entry(3, "", "read_file"),
        entry(5, "完整答案", "offer_next_step"),
        entry(7, "收尾"),
    ]
    assert manager.final_reply(REPLY) == ([5, 7], "完整答案\n\n收尾")
