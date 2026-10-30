"""完整快照的历史窗口与更早历史分页。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.state.snapshot_window import history_page, window_snapshot
from app.config.constant import Constant


def _messages(turns: int, answers_per_turn: int = 1) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in range(turns):
        messages.append({"id": f"u-{turn}", "role": "user", "parts": [], "runId": turn + 1, "outcome": None})
        for answer in range(answers_per_turn):
            messages.append(
                {"id": f"a-{turn}-{answer}", "role": "assistant", "parts": [], "runId": turn + 1, "outcome": None}
            )
    return messages


def _state(messages: list[dict[str, Any]]) -> ConversationStateSnapshot:
    state = empty_snapshot("t")
    state["messages"] = cast(Any, messages)
    return state


@pytest.fixture(autouse=True)
def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Constant.Transport, "HISTORY_WINDOW_MESSAGES", 5)


def test_short_history_is_sent_whole() -> None:
    state = _state(_messages(2))
    assert window_snapshot(state) is state


def test_window_keeps_the_tail_and_starts_at_a_user_message() -> None:
    state = _state(_messages(4, answers_per_turn=2))  # 12 条，每轮 3 条
    windowed = window_snapshot(state)

    # 裁到最后 5 条会从第 7 条（一条回答）开始，往前对齐到第 6 条（第 3 轮的提问）。
    assert windowed["messageOffset"] == 6
    assert [message["id"] for message in windowed["messages"]] == [
        "u-2", "a-2-0", "a-2-1", "u-3", "a-3-0", "a-3-1"
    ]
    assert windowed["title"] == "t" and state["messageOffset"] == 0
    assert len(state["messages"]) == 12
    validate_snapshot(windowed)


def test_history_page_returns_the_turns_before_end() -> None:
    messages = cast(Any, _messages(4, answers_per_turn=2))

    page = history_page(messages, 6)
    assert page["messageOffset"] == 0
    assert [message["id"] for message in page["messages"]] == ["u-0", "a-0-0", "a-0-1", "u-1", "a-1-0", "a-1-1"]
    assert history_page(messages, 0) == {"messages": [], "messageOffset": 0}


def test_history_page_rejects_positions_from_a_replaced_snapshot() -> None:
    with pytest.raises(ValueError):
        history_page(cast(Any, _messages(1)), 3)


def test_without_any_user_message_the_window_keeps_its_raw_start() -> None:
    messages = [{"id": f"a-{index}", "role": "assistant", "parts": [], "runId": 1, "outcome": None} for index in range(8)]
    windowed = window_snapshot(_state(messages))
    assert windowed["messageOffset"] == 3
