"""Snapshot 变化的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass

from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)


@dataclass(frozen=True)
class SnapshotChange:
    """一次已提交的 snapshot 变化。"""

    task_id: int
    state: ConversationStateSnapshot
    mutations: tuple[ConversationStateMutation, ...]
