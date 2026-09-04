"""Conversation task snapshot mutation 的进程内订阅。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationStateMutation,
    ConversationTaskSnapshotService,
    SnapshotChange,
)


class ConversationRunSubscriptionService:
    """按 canonical snapshot 变化订阅 task 状态。"""

    def __init__(
        self,
        snapshot_service: ConversationTaskSnapshotService | None = None,
    ) -> None:
        """初始化只读 task snapshot 依赖。

        参数:
            snapshot_service: 可选的进程内 snapshot owner；未提供时创建默认实例。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 snapshot owner 引用，不执行数据库写入。
        """
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
        """订阅已提交的局部 mutation，不重建完整消息数组。

        参数:
            task_id: 对话任务标识。
            run_id: 需要订阅的 Conversation Run 标识。
            is_cancelled: 返回 HTTP stream 是否已取消的回调。
            is_terminal: 可选的执行器终态回调，用于无 mutation 时结束订阅。
            poll_seconds: 等待进程内通知的最长轮询间隔。

        返回:
            已提交的 snapshot change 异步迭代器。

        异常:
            无；缺少 snapshot 时结束迭代。

        副作用:
            临时注册进程内订阅者，并在迭代结束时注销。
        """
        queue, unsubscribe = self._snapshots.subscribe(task_id)
        try:
            current = self._snapshots.load(task_id)
            if current is None or current["run"]["runId"] != run_id:
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
                    current = self._snapshots.load(task_id)
                    if current is not None and current != last_state:
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
