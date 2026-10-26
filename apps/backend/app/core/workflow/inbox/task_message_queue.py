"""Task 外部消息队列：持久化、按 Task 分片的阻塞队列，是待并入消息的唯一事实源。

消息行存在 SQLite（``task_messages``），阻塞只是进程内的唤醒：:meth:`TaskMessageQueue.pending`
挂起到该 Task 出现水位之后的新消息，:meth:`TaskMessageQueue.put` 提交后唤醒同一 Task 的全部等待方。

与 ``queue.Queue`` / ``asyncio.Queue`` 的区别是取出不删除：投递不删行，``prepare_input`` 写
context 时在同一事务内 :meth:`TaskMessageQueue.ack` 删除；执行结束、取消或进程重启时 mailbox
随之丢弃，未确认的行仍在这里，交接或下次投递时重新送达。因此 ``pending`` 按水位（调用方看过的
最大消息 id）阻塞，而不是按「非空」：留在队列里尚未被消费的行（如运行中排队的 ``next_run``）
否则会让它每次都立刻返回，调用方空转、重复投递。

只负责入队、按序读取、原地更新与确认删除，不承载投递策略、输入解析或交接规则。唤醒器是进程内
状态，不跨重启；重启后首次 ``pending`` 直接读到遗留的行。
"""

from __future__ import annotations

import asyncio
import threading
from typing import cast

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.workflow.inbox.task_message import TaskMessage
from app.storage.crud.task_message_crud import TaskMessageCrud
from app.storage.store_engines import main_session_factory

# 一个等待方：所在事件循环与它的唤醒事件。put 可能在工作线程执行，只能经
# ``loop.call_soon_threadsafe`` 置位 asyncio 事件。
_Waiter = tuple[asyncio.AbstractEventLoop, asyncio.Event]


class TaskMessageQueue:
    def __init__(self) -> None:
        self._messages = TaskMessageCrud()
        # task_id → 正在 ``pending`` 里挂起的等待方；任意线程读写，由 ``_lock`` 保护。
        self._waiters: dict[int, set[_Waiter]] = {}
        self._lock = threading.Lock()

    def put(self, task_id: int, message: TaskMessage, *, session: Session | None = None) -> int:
        """持久化一条消息并唤醒该 Task 的等待方，返回消息身份；任意线程可调。

        传入 ``session`` 时加入调用方事务（编辑的移出与入队），唤醒推迟到该事务提交之后：提交前
        唤醒的话，等待方读不到这一行又会睡回去，这次唤醒就丢了；事务回滚则不唤醒。
        """
        if session is not None:
            [message_id] = self._messages.append(session, [(task_id, message)])
            event.listen(session, "after_commit", lambda _s: self._notify(task_id), once=True)
        else:
            with main_session_factory().begin() as own:
                [message_id] = self._messages.append(own, [(task_id, message)])
            self._notify(task_id)
        log.info(
            "task_message_enqueued",
            extra={
                "msg": "消息已持久化，等待投递",
                "data": {
                    "task_id": task_id,
                    "message_id": message_id,
                    "kind": message.kind,
                    "delivery": message.delivery,
                },
            },
        )
        return message_id

    async def pending(self, task_id: int, after_id: int = 0) -> list[TaskMessage]:
        """按入队顺序返回该 Task 全部未确认的消息；还没有 id 大于 ``after_id`` 的消息时挂起，直到
        ``put`` 唤醒。

        返回的是全部未确认消息（含水位之前的），调用方按 id 区分新到达的部分；不删除返回的消息
        （见模块 docstring）。先登记等待方、每轮读库前清唤醒标记再读：读库之后到达的 ``put`` 一定
        会置位标记，不会丢唤醒；多次 ``put`` 合并为一次读库。调用方取消时等待方随之注销。
        """
        waiter: _Waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.setdefault(task_id, set()).add(waiter)
        try:
            while True:
                waiter[1].clear()
                messages = await asyncio.to_thread(self.pending_nowait, task_id)
                if any(cast(int, message.id) > after_id for message in messages):
                    return messages
                await waiter[1].wait()
        finally:
            with self._lock:
                waiters = self._waiters.get(task_id)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        del self._waiters[task_id]

    def pending_nowait(self, task_id: int) -> list[TaskMessage]:
        """不阻塞地按入队顺序读取该 Task 全部未确认的消息（可能为空）。"""
        with main_session_factory()() as session:
            return self._messages.list_pending(session, task_id)

    def update(self, task_id: int, message_id: int, message: TaskMessage) -> bool:
        """原地更新仍在队列中的消息正文，身份与属性不变；已被并入（行已删除）时返回 ``False``。

        与 ``prepare_input`` 的确认并发时由这一次条件更新裁决，不另加前置检查。行数不变，不唤醒。
        """
        with main_session_factory().begin() as session:
            return self._messages.update_body_if_exists(session, task_id, message_id, message)

    def ack(self, session: Session, ids: list[int]) -> dict[int, TaskMessage]:
        """在调用方事务内确认（删除）已并入 context 的行，返回实际删除的行内容（按 id）。

        与 context 写入同一事务提交，消息不会丢也不会被并入两次；不在返回值里的 id 已被别的
        执行确认过，调用方应跳过对应消息。调用方应写入返回的内容而不是 mailbox 里的副本：排队
        期间的原地编辑只更新了行。
        """
        return self._messages.delete_returning(session, ids)

    def _notify(self, task_id: int) -> None:
        """唤醒该 Task 的全部等待方；在提交之后调用，等待方醒来一定读得到新行。"""
        with self._lock:
            waiters = list(self._waiters.get(task_id, ()))
        for loop, wakeup in waiters:
            loop.call_soon_threadsafe(wakeup.set)
