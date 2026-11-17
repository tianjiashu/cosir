"""会话交付清单（snapshot 顶层 ``deliverables``）：按 key 去重留最后一次、最近在前、继承计入、窗口外仍在。"""

from typing import Any

import pytest

from app.assistant_transport.state.conversation_state_deliverable import (
    project_session_deliverables,
    validate_session_deliverables,
)
from app.assistant_transport.state.snapshot_window import window_snapshot
from app.config.constant import Constant


def deliverable(key: str) -> dict[str, Any]:
    return {
        "type": "file",
        "key": key,
        "caption": None,
        "workspacePath": key.lstrip("/"),
        "absolutePath": key,
        "kind": "text",
        "size": 1,
        "width": None,
        "height": None,
    }


def message(
    index: int, keys: list[str], *, inherited: bool = False, role: str = "assistant"
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": "完"}]
    if keys:
        parts.insert(
            0,
            {
                "type": "tool-call",
                "toolCallId": f"c{index}",
                "deliverables": [deliverable(key) for key in keys],
            },
        )
    return {"id": f"m{index}", "role": role, "parts": parts, "turn": index, "inherited": inherited}


# 潜在缺陷：同一文件交付两次出现两条；顺序按第一次交付而不是最近一次；手动 fork 继承的卡片在侧栏里缺失。
def test_dedupes_by_key_keeps_the_latest_and_lists_newest_first() -> None:
    messages = [
        message(1, ["/w/a", "/w/b"], inherited=True),
        message(2, []),
        message(3, ["/w/c", "/w/a"]),
    ]
    assert [item["key"] for item in project_session_deliverables(messages)] == [
        "/w/a",
        "/w/c",
        "/w/b",
    ]  # type: ignore[arg-type]
    assert project_session_deliverables([]) == []


def test_window_cut_keeps_deliverables_from_messages_outside_the_window() -> None:
    """完整快照只按窗口裁剪 messages：早期回合的交付物不在窗口里，清单仍完整。"""
    count = Constant.Transport.HISTORY_WINDOW_MESSAGES + 4
    messages = [message(0, ["/w/early"])] + [
        message(index, [], role="user" if index % 2 else "assistant") for index in range(1, count)
    ]
    state: dict[str, Any] = {
        "messages": messages,
        "messageOffset": 0,
        "deliverables": project_session_deliverables(messages),
    }  # type: ignore[arg-type]
    windowed = window_snapshot(state)  # type: ignore[arg-type]
    assert windowed["messageOffset"] > 0
    assert [item["key"] for item in windowed["deliverables"]] == ["/w/early"]


def test_validation_rejects_duplicate_keys_and_bad_entries() -> None:
    validate_session_deliverables([deliverable("/w/a"), deliverable("/w/b")])
    with pytest.raises(ValueError, match="unique"):
        validate_session_deliverables([deliverable("/w/a"), deliverable("/w/a")])
    with pytest.raises(ValueError):
        validate_session_deliverables([{**deliverable("/w/a"), "kind": "spreadsheet"}])
