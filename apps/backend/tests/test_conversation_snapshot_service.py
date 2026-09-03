"""ConversationStateSnapshot 增量 mutation 的纯逻辑测试。"""

import asyncio

import pytest

from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.service.conversation_snapshot_service import (
    ConversationStateMutation,
    _apply_mutation,
)


def _snapshot() -> ConversationStateSnapshot:
    """构造一份最小但完整的快照。"""

    return {
        "messages": [
            {
                "id": "message-1",
                "role": "assistant",
                "status": "running",
                "endReason": None,
                "createdAt": "2026-01-01T00:00:00+00:00",
                "parts": [{"type": "text", "text": "hello"}],
            }
        ],
        "run": {"runId": 1, "status": "running"},
        "error": None,
    }


def test_set_and_append_text_apply_to_the_same_snapshot() -> None:
    """set 更新结构字段，append-text 只追加目标文本。"""

    state = _snapshot()

    _apply_mutation(
        state,
        ConversationStateMutation("append-text", ("messages", 0, "parts", 0, "text"), " world"),
    )
    _apply_mutation(
        state,
        ConversationStateMutation("set", ("messages", 0, "status"), "complete"),
    )

    assert state["messages"][0]["parts"][0]["text"] == "hello world"
    assert state["messages"][0]["status"] == "complete"


def test_set_can_append_a_new_message_or_part_at_the_next_index() -> None:
    """set 支持以数组长度为路径追加新的消息或 part。"""

    state = _snapshot()
    new_message = {
        "id": "message-2",
        "role": "user",
        "status": "complete",
        "endReason": None,
        "createdAt": "2026-01-01T00:00:01+00:00",
        "parts": [],
    }
    new_part = {
        "type": "tool-call",
        "toolCallId": "call-1",
        "toolName": "read_file",
        "status": "pending",
    }

    _apply_mutation(state, ConversationStateMutation("set", ("messages", 1), new_message))
    _apply_mutation(
        state,
        ConversationStateMutation("set", ("messages", 0, "parts", 1), new_part),
    )

    assert state["messages"][1]["id"] == "message-2"
    assert state["messages"][0]["parts"][1]["toolCallId"] == "call-1"


@pytest.mark.asyncio
async def test_subscription_starts_with_one_complete_snapshot() -> None:
    """订阅建立后首个 change 必须是完整 root set，而不是等待后续 mutation。"""

    from app.assistant_transport.service.conversation_run_subscription_service import (
        ConversationRunSubscriptionService,
    )

    state = _snapshot()

    class FakeStateService:
        """返回固定快照的只读 state service。"""

        def build_run_state(self, task_id: int, run_id: int) -> ConversationStateSnapshot:
            return state

    class FakeSnapshotService:
        """提供不含数据库的订阅桩。"""

        def subscribe(self, task_id: int):
            return asyncio.Queue(), lambda: None

    stream = ConversationRunSubscriptionService(
        FakeStateService(),  # type: ignore[arg-type]
        FakeSnapshotService(),  # type: ignore[arg-type]
    ).stream(1, 1, lambda: False)
    first = await anext(stream)
    await stream.aclose()

    assert first.mutations[0].kind == "set"
    assert first.mutations[0].path == ()
    assert first.state == state
