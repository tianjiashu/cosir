"""Backend 进程内已物化的 Task runtime space 注册表。

负责在进程生命周期内稳定持有每个 task_id 对应的 ``TaskRuntimeSpace`` 单例，
并提供单个及整体清理入口。单个 space 的资源构成与 Task 操作锁见
``app.task_runtime.task_runtime_space``。

每个 space 持有 Task 的调度者 ``TaskActor``：space 物化即拉起它的 loop，常驻到删除或关闭。space
可能在工作线程物化，loop 只能回到事件循环上拉起，所以注册表在启动时 :meth:`bind_loop` 记下事件
循环，之前已物化的 space 在那时补拉起。往外部队列投递一律经 :meth:`enqueue`：先确保 space（及其
loop）存在，否则重启后没被访问过的 Task 收到消息时没有 loop 在等。
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from app.task_runtime.task_runtime_space import TaskRuntimeSpace

from app.config.logging.logger import log

if TYPE_CHECKING:
    from concurrent.futures import Future

    from sqlalchemy.orm import Session

    from app.core.workflow.cancel.conversation_run_cancellation_registry import CancelReason
    from app.core.workflow.message_queue.task_message import TaskMessage


class TaskRuntimeSpaceRegistry:
    """管理 backend 进程内已经物化的 Task runtime space。"""

    def __init__(self) -> None:
        """创建可供 backend 多线程访问的空 registry。"""

        self._spaces: dict[int, TaskRuntimeSpace] = {}
        self._deleted_task_ids: set[int] = set()
        self._guard = threading.Lock()
        # actor loop 所在的事件循环；``bind_loop`` 之前物化的 space 暂不拉起。
        self._loop: asyncio.AbstractEventLoop | None = None

    def get_or_create(self, task_id: int) -> TaskRuntimeSpace:
        """返回 ``task_id`` 稳定对应的 runtime space。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            已存在或新创建的 ``TaskRuntimeSpace``；同一 ``task_id`` 始终返回同一实例。

        异常:
            KeyError: 如果该 Task 已经完成删除并被标记为不可重新物化。

        副作用:
            首次为该 ``task_id`` 请求时构造并登记 ``TaskRuntimeSpace``；后续请求
            直接返回缓存实例。登记动作在 ``_guard`` 保护下进行，线程安全。
        """

        with self._guard:
            if task_id in self._deleted_task_ids:
                raise KeyError(task_id)
            space = self._spaces.get(task_id)
            if space is None:
                space = TaskRuntimeSpace(task_id)
                self._spaces[task_id] = space
                # 事件循环已关闭（进程退出中）时不再拉起：没有可运行 actor 的循环。
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(space.actor.start)
            return space

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记下 actor loop 所在的事件循环，并拉起此前已物化的 space 的 actor；lifespan 启动时
        调用。"""

        with self._guard:
            self._loop = loop
            spaces = tuple(self._spaces.values())
        for space in spaces:
            loop.call_soon_threadsafe(space.actor.start)

    def enqueue(
        self, task_id: int, message: TaskMessage, *, session: Session | None = None
    ) -> int:
        """确保 Task 的 space 与 actor 已拉起，再把消息写入外部队列；返回消息身份，任意线程可调。

        异常:
            KeyError: Task 不存在或已删除。
        """
        from app.service.depends import get_task_message_queue

        self.get_or_create(task_id)
        return get_task_message_queue().put(task_id, message, session=session)

    def cancel(self, task_id: int, reason: CancelReason) -> None:
        """从任意线程发起停止一个 Task（上游停止下游、Team 取消节点）；只发起，不等待。

        交给该 Task 的 actor 判定：有执行者打信号，挂起的 Run 直接迁移，没有可停止的对象什么也不做。
        不在这里直接打取消信号——没有执行者时登记的信号没人取走，会杀死下一个 Run。不等待：调用方
        可能正持有自己的 Task 闸门，等对方的闸门会让父子互等。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        space = self.get_or_create(task_id)
        future: Future[None] = asyncio.run_coroutine_threadsafe(space.actor.cancel(reason), loop)
        future.add_done_callback(lambda done: _log_cancel_failure(task_id, done))

    async def stop(self, task_ids: set[int] | list[int]) -> None:
        """删除前停下这些 Task 的 actor：停止执行并等它落定，再停下 loop。须在删除闸门外调用。"""

        for task_id in task_ids:
            space = self.get(task_id)
            if space is not None:
                await space.actor.stop()

    async def aclose(self) -> None:
        """进程关闭：取消全部 actor 的执行与 loop（不调度后续 Run），再清空注册表。"""

        with self._guard:
            spaces = tuple(self._spaces.values())
            self._loop = None
        await asyncio.gather(*(space.actor.abort() for space in spaces), return_exceptions=True)
        self.close()

    def get(self, task_id: int) -> TaskRuntimeSpace | None:
        """返回已经登记的 Task runtime space，不触发惰性创建。"""

        with self._guard:
            return self._spaces.get(task_id)

    def existing_spaces(self) -> tuple[TaskRuntimeSpace, ...]:
        """返回当前进程中已经物化的 Task runtime space 快照。

        返回:
            按登记快照复制出的 space 元组。返回值不会包含尚未运行过的 Task，也不会触发新的
            space 创建；调用方可以在释放 registry 锁后执行通知过滤和入队。

        异常:
            无。

        副作用:
            无。返回的是既有 ``TaskRuntimeSpace`` 实例引用，space 自身生命周期仍由 registry
            管理。
        """

        with self._guard:
            return tuple(self._spaces.values())

    def mark_deleted(self, task_id: int) -> None:
        """禁止已删除 Task 再次惰性创建 runtime space。"""

        with self._guard:
            self._deleted_task_ids.add(task_id)

    def unload(
        self,
        task_id: int,
        *,
        expected_space: TaskRuntimeSpace | None = None,
    ) -> None:
        """卸载一个已经停止使用的 Task runtime space。

        参数:
            task_id: 待卸载任务标识。
            expected_space: 可选的身份校验对象；传入时只卸载仍指向该实例的条目，
                防止旧删除流程误删后来重新创建的 runtime space。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内 registry 删除一个 Task runtime space；不会停止执行中的任务。
        """

        with self._guard:
            current = self._spaces.get(task_id)
            if current is None:
                return
            if expected_space is not None and current is not expected_space:
                return
            self._spaces.pop(task_id, None)

    def close(self) -> None:
        """所有执行停止后清理进程内的运行时投影。"""

        with self._guard:
            self._spaces.clear()
            self._deleted_task_ids.clear()
            self._loop = None


def _log_cancel_failure(task_id: int, done: Future[None]) -> None:
    if not done.cancelled() and done.exception() is not None:
        log.error(
            "task_cancel_failed",
            extra={"msg": "发起停止 Task 失败", "data": {"task_id": task_id}},
            exc_info=done.exception(),
        )


#: 进程级单例：多 space 的生命周期在 backend 进程内稳定持有一份。
task_runtime_spaces: TaskRuntimeSpaceRegistry = TaskRuntimeSpaceRegistry()
