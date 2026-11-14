"""Backend 进程内已物化的 Session runtime space 注册表。

负责在进程生命周期内稳定持有每个 session_id 对应的 ``SessionRuntimeSpace`` 单例，
并提供单个及整体清理入口。单个 space 的资源构成与 Session 操作锁见
``app.session_runtime.session_runtime_space``。

每个 space 持有 Session 的调度者 ``SessionActor``：space 物化即拉起它的 loop，常驻到删除或关闭。space
可能在工作线程物化，loop 只能回到事件循环上拉起，所以注册表在启动时 :meth:`bind_loop` 记下事件
循环，之前已物化的 space 在那时补拉起。往外部队列投递一律经 :meth:`enqueue`：先确保 space（及其
loop）存在，否则重启后没被访问过的 Session 收到消息时没有 loop 在等。

注册表只负责持有 space 与 actor 的生命周期（拉起、删除前停止、进程关闭），不承载取消语义：停止
一个 Session 经它找到 actor 后调用 ``actor.cancel`` / ``actor.request_cancel``。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.core.workflow.message_queue.session_message import NoticeBody, SessionMessage
from app.session_runtime.session_runtime_space import SessionRuntimeSpace

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DbSession


class SessionRuntimeSpaceRegistry:
    """管理 backend 进程内已经物化的 Session runtime space。"""

    def __init__(self) -> None:
        """创建可供 backend 多线程访问的空 registry。"""

        self._spaces: dict[int, SessionRuntimeSpace] = {}
        self._deleted_session_ids: set[int] = set()
        self._guard = threading.Lock()
        # actor loop 所在的事件循环；``bind_loop`` 之前物化的 space 暂不拉起。
        self._loop: asyncio.AbstractEventLoop | None = None

    def get_or_create(self, session_id: int) -> SessionRuntimeSpace:
        """返回 ``session_id`` 稳定对应的 runtime space。

        参数:
            session_id: 会话标识（整数 id）。

        返回:
            已存在或新创建的 ``SessionRuntimeSpace``；同一 ``session_id`` 始终返回同一实例。

        异常:
            KeyError: 如果该 Session 已经完成删除并被标记为不可重新物化。

        副作用:
            首次为该 ``session_id`` 请求时构造并登记 ``SessionRuntimeSpace``；后续请求
            直接返回缓存实例。登记动作在 ``_guard`` 保护下进行，线程安全。
        """

        with self._guard:
            if session_id in self._deleted_session_ids:
                raise KeyError(session_id)
            space = self._spaces.get(session_id)
            if space is None:
                space = SessionRuntimeSpace(session_id)
                self._spaces[session_id] = space
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
        self, session_id: int, message: SessionMessage, *, db: DbSession | None = None
    ) -> int:
        """确保 Session 的 space 与 actor 已拉起，再把消息写入外部队列；返回消息身份，任意线程可调。

        异常:
            KeyError: Session 不存在或已删除。
        """
        from app.service.depends import get_session_message_queue

        self.get_or_create(session_id)
        return get_session_message_queue().put(session_id, message, db=db)

    async def stop(self, session_ids: set[int] | list[int]) -> None:
        """删除前停下这些 Session 的 actor：停止执行并等它落定，再停下 loop。须在删除闸门外调用。

        停止即删除的墓碑：之后 loop 不再推进、恢复被拒绝，入队的消息只留在队列里随删除级联清掉。
        删除没提交时须调用 :meth:`restart_stopped` 回滚。
        """

        for session_id in session_ids:
            space = self.get(session_id)
            if space is not None:
                await space.actor.stop()

    def restart_stopped(self, session_ids: set[int] | list[int]) -> None:
        """删除失败后为这些 Session 换上新 actor；须在事件循环线程调用。

        只换已被 :meth:`stop` 停下的：停止之后才物化的 space 带着正常运行的 actor，换掉会让两个
        loop 同时调度一个 Session。
        """

        for session_id in session_ids:
            space = self.get(session_id)
            if space is not None and space.actor.stopped.is_set():
                space.restart_actor()

    async def aclose(self) -> None:
        """进程关闭：取消全部 actor 的执行与 loop（不调度后续 Run），再清空注册表。"""

        with self._guard:
            spaces = tuple(self._spaces.values())
            self._loop = None
        await asyncio.gather(*(space.actor.abort() for space in spaces), return_exceptions=True)
        self.close()

    def get(self, session_id: int) -> SessionRuntimeSpace | None:
        """返回已经登记的 Session runtime space，不触发惰性创建。"""

        with self._guard:
            return self._spaces.get(session_id)

    def broadcast_notice(
        self, notice: NoticeBody, reaches: Callable[[SessionRuntimeSpace], bool]
    ) -> int:
        """把一条 Session 级通知写入命中的已物化 Session 的外部队列（每个 Session 一笔事务），返回投递数。

        只发给已物化的 Session：它们的 actor 已在监听，通知随下一次推进写入。不创建新的 space、不启动
        Run、不打断正在进行的模型请求。``reaches`` 抛出的异常原样上浮。
        """

        session_ids = [space.session_id for space in self.existing_spaces() if reaches(space)]
        for session_id in session_ids:
            self.enqueue(session_id, SessionMessage.of(notice))
        return len(session_ids)

    def existing_spaces(self) -> tuple[SessionRuntimeSpace, ...]:
        """返回当前进程中已经物化的 Session runtime space 快照。

        返回:
            按登记快照复制出的 space 元组。返回值不会包含尚未运行过的 Session，也不会触发新的
            space 创建；调用方可以在释放 registry 锁后执行通知过滤和入队。

        异常:
            无。

        副作用:
            无。返回的是既有 ``SessionRuntimeSpace`` 实例引用，space 自身生命周期仍由 registry
            管理。
        """

        with self._guard:
            return tuple(self._spaces.values())

    def mark_deleted(self, session_id: int) -> None:
        """记下 Session 已在本进程删除：之后不再物化它的 space，快照读取与订阅一律拒绝。

        「本进程里这个 Session 已删除」只记在这里，Transport 的快照服务也读这一份。
        """

        with self._guard:
            self._deleted_session_ids.add(session_id)

    def is_deleted(self, session_id: int) -> bool:
        with self._guard:
            return session_id in self._deleted_session_ids

    def unload(
        self,
        session_id: int,
        *,
        expected_space: SessionRuntimeSpace | None = None,
    ) -> None:
        """Session 删除提交后卸载它的 runtime space，并停下仍在监听的 actor loop。

        ``expected_space`` 传入时只卸载仍指向该实例的条目，防止旧删除流程误删后来重新创建的
        space。普通删除在删除前已经 :meth:`stop` 过 actor，这里不再有事可做；临时 Session 清理为取
        Session 闸门物化了 space、actor 还在等新消息，不在这里停下，loop 就永远挂在已删除 Session 的
        队列上。停止投递到 actor 所在的事件循环、不等完成：调用方可能在工作线程里。
        """

        with self._guard:
            current = self._spaces.get(session_id)
            if current is None:
                return
            if expected_space is not None and current is not expected_space:
                return
            self._spaces.pop(session_id, None)
            loop = self._loop
        if not current.actor.stopped.is_set() and loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(current.actor.abort(), loop)

    def close(self) -> None:
        """所有执行停止后清理进程内的运行时投影。"""

        with self._guard:
            self._spaces.clear()
            self._deleted_session_ids.clear()
            self._loop = None


#: 进程级单例：多 space 的生命周期在 backend 进程内稳定持有一份。
session_runtime_spaces: SessionRuntimeSpaceRegistry = SessionRuntimeSpaceRegistry()
