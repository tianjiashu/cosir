"""ConversationState 快照 mutation 的进程内订阅。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.assistant_transport.service.conversation_snapshot_service import (
    ConversationStateMutation,
    ConversationTaskSnapshotService,
    SnapshotChange,
)
from app.service.task.conversation_state_service import ConversationStateService


class ConversationRunSubscriptionService:
    """按 canonical snapshot 变化订阅 task 状态。"""

    def __init__(
        self,
        state_service: ConversationStateService | None = None,
        snapshot_service: ConversationTaskSnapshotService | None = None,
    ) -> None:
        """初始化只读 state 投影依赖。"""
        self._state = state_service or ConversationStateService()
        self._snapshots = snapshot_service or ConversationTaskSnapshotService()

    async def stream(
        self,
        task_id: int,
        run_id: int,
        is_cancelled: Callable[[], bool],
        is_terminal: Callable[[], bool | Awaitable[bool]] | None = None,
        *,
        poll_seconds: float = 0.05,
    ) -> AsyncIterator[SnapshotChange]:
        """订阅已提交的局部 mutation，不重建完整消息数组。"""
        queue, unsubscribe = self._snapshots.subscribe(task_id)
        try:
            current = self._state.build_run_state(task_id, run_id)
            if current["run"]["runId"] != run_id:
                return
            last_state = current
            # Every connection starts with exactly one complete snapshot. Later
            # changes are the committed local mutations delivered by the notifier.
            yield SnapshotChange(
                task_id,
                current,
                (ConversationStateMutation("set", (), current),),
            )
            if current["run"]["status"] in {"completed", "failed", "cancelled"}:
                return
            while not is_cancelled():
                try:
                    change = await asyncio.wait_for(queue.get(), timeout=poll_seconds)
                except TimeoutError:
                    current = self._state.build_run_state(task_id, run_id)
                    if current != last_state:
                        last_state = current
                        yield SnapshotChange(
                            task_id,
                            current,
                            (ConversationStateMutation("set", (), current),),
                        )
                        if current["run"]["status"] in {
                            "completed",
                            "failed",
                            "cancelled",
                        }:
                            return
                    if is_terminal is not None:
                        terminal = is_terminal()
                        if isinstance(terminal, Awaitable):
                            terminal = await terminal
                        if terminal:
                            return
                    continue
                if change.task_id != task_id:
                    continue
                last_state = change.state
                if change.state["run"]["runId"] != run_id:
                    return
                yield change
                if change.state["run"]["runId"] == run_id and change.state["run"]["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return
        finally:
            unsubscribe()
