"""Conversation Run 的 canonical state 订阅服务。

订阅只把 ``conversation_changes`` 当作唤醒提示；真正发送给 Assistant Transport 的内容
始终重新读取 canonical facts。进程内 notifier 尚未接入时使用短轮询，仍具备断线/进程重启
后的可恢复语义。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.service.task.conversation_state_service import ConversationStateService
from app.service.task.conversation_state_snapshot import ConversationStateSnapshot
from app.storage.crud.conversation_change_crud import ConversationChangeCrud


class ConversationChangeNotifier:
    """进程内提交后唤醒器；数据库轮询仍是崩溃恢复兜底。"""

    def __init__(self) -> None:
        """初始化按 task 隔离的 condition。"""
        self._conditions: dict[tuple[int, int], asyncio.Condition] = {}

    def _condition(self, task_id: int) -> asyncio.Condition:
        """返回 task 对应的唤醒条件。"""
        loop = asyncio.get_running_loop()
        return self._conditions.setdefault((id(loop), task_id), asyncio.Condition())

    async def notify(self, task_id: int) -> None:
        """唤醒等待该 task revision 的订阅者。"""
        condition = self._condition(task_id)
        async with condition:
            condition.notify_all()

    async def wait(self, task_id: int, wait_seconds: float) -> None:
        """等待提交通知，超时后由调用方查询数据库。"""
        condition = self._condition(task_id)
        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), wait_seconds)
            except TimeoutError:
                return


_conversation_change_notifier = ConversationChangeNotifier()


def get_conversation_change_notifier() -> ConversationChangeNotifier:
    """返回进程级 conversation change notifier。"""
    return _conversation_change_notifier


class ConversationRunSubscriptionService:
    """按 revision 订阅 task 的 canonical state 快照。"""

    def __init__(
        self,
        state_service: ConversationStateService | None = None,
        change_crud: ConversationChangeCrud | None = None,
        notifier: ConversationChangeNotifier | None = None,
    ) -> None:
        """初始化只读 state 投影依赖。"""
        self._state = state_service or ConversationStateService()
        self._changes = change_crud or ConversationChangeCrud()
        self._notifier = notifier or get_conversation_change_notifier()

    async def stream(
        self,
        task_id: int,
        run_id: int,
        is_cancelled: Callable[[], bool],
        is_terminal: Callable[[], bool | Awaitable[bool]] | None = None,
        *,
        poll_seconds: float = 0.05,
        after_revision: int = -1,
    ) -> AsyncIterator[ConversationStateSnapshot]:
        """持续产生 revision 前进的 canonical state，直到 run 进入终态。

        参数:
            task_id: task/Conversation Thread 标识。
            run_id: 当前 Conversation Run 标识。
            is_cancelled: HTTP stream 是否已关闭的瞬时检查，不等同于业务取消。
            is_terminal: 可选的执行器状态检查；用于运行尚未来得及提交事实时安全结束订阅。
            poll_seconds: 无 notifier 时的最大轮询间隔。

        返回:
            每次 revision 前进时重新从事实表构造的完整 state。

        异常:
            数据库读取异常向调用方传播。

        副作用:
            只读访问 canonical state，不写入数据库；客户端断开后停止订阅。
        """
        last_revision = after_revision
        while not is_cancelled():
            snapshot = self._state.build_run_state(task_id, run_id)
            current_run = snapshot["run"]
            if current_run["runId"] == run_id:
                # change 只是提交后的唤醒索引；任何实际输出都重新从 facts snapshot 读取。
                changes = self._changes.list_after(task_id, last_revision)
                if last_revision < 0 or changes:
                    if snapshot["revision"] <= last_revision:
                        await asyncio.sleep(poll_seconds)
                        continue
                    last_revision = snapshot["revision"]
                    yield snapshot
                if current_run["status"] in {"completed", "failed", "cancelled"}:
                    return
            if is_terminal is not None:
                terminal = is_terminal()
                if isinstance(terminal, Awaitable):
                    terminal = await terminal
                if terminal:
                    return
            await self._notifier.wait(task_id, poll_seconds)
