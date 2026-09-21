"""Assistant Transport frame 的进程内中性数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)


FrameKind = Literal["full", "mutation", "resync_required"]


@dataclass(frozen=True, slots=True)
class TransportFrame:
    """一次已经提交、可被 SSE 订阅者消费的 snapshot frame。

    ``full`` frame 携带完整 state；普通 ``mutation`` frame 只携带 mutation，避免在
    每个 token 到达时复制并编码整个 task snapshot。``resync_required`` 是连接级背压
    信号，要求客户端通过 attach 重新取得 full frame。这里没有版本游标、revision、
    epoch 或 generation 字段；顺序由单个连接的 subscriber 队列维护。
    """

    task_id: int
    kind: FrameKind
    mutations: tuple[ConversationStateMutation, ...]
    state: ConversationStateSnapshot | None = None
    source_run_id: int | None = None
    current_run_id: int | None = None
    current_run_status: str | None = None
    resync_reason: str | None = None


__all__ = ["FrameKind", "TransportFrame"]
