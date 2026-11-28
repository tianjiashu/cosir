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
    每个 token 到达时复制并编码整个 session snapshot。``resync_required`` 是连接级背压
    信号，要求客户端通过 attach 重新取得 full frame。帧之间的顺序由单个连接的 subscriber 队列维护。

    ``revision`` 是这个 Session 已发布到第几帧（工作副本锁内递增，``full`` 帧取当时的值）：它只为
    SSE 之外的那条通道——HTTP 历史页——提供共同的先后依据。历史页带上读取那一刻的 revision，前端据此
    分辨在途增量是否已含在页里（见前端 ``TransportFrameStore.completeHistoryFetch``）。连接级的
    ``resync_required`` 不属于任何 Session 的发布序列，revision 为 0。
    """

    session_id: int
    kind: FrameKind
    mutations: tuple[ConversationStateMutation, ...] = ()
    state: ConversationStateSnapshot | None = None
    resync_reason: str | None = None
    revision: int = 0


__all__ = ["FrameKind", "TransportFrame"]
