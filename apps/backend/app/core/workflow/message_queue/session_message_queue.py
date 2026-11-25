"""Session 外部消息队列：持久化、按 Session 分片的阻塞队列，是待并入消息的唯一事实源。

消息行存在 SQLite（``session_messages``），阻塞只是进程内的唤醒：:meth:`SessionMessageQueue.wait_unhandled`
挂起到该 Session 出现调用方还没判定过的消息，:meth:`SessionMessageQueue.put` 与
:meth:`SessionMessageQueue.promote` 提交后唤醒同一 Session 的全部等待方。

与 ``queue.Queue`` / ``asyncio.Queue`` 的区别是取出不删除：投递不删行，``prepare_input`` 写
context 时在同一事务内 :meth:`SessionMessageQueue.ack` 删除；执行结束、取消或进程重启时 mailbox
随之丢弃，未确认的行仍在这里，交接或下次投递时重新送达。因此等待按「有没有调用方没判定过的消息」，
而不是按「非空」：留在队列里尚未被消费的行（如运行中排队的 ``next_run``）否则会让它每次都立刻返回，
调用方空转、重复投递。

身份与变化分开：行 id 是消息的稳定身份（排队、并入、撤回与投影都按它寻址，从不更换）；「调用方还没
判定过」由两部分构成——水位（调用方判定过的最大 id）之后新入队的行，以及登记在进程内待重判集合里的
行（用户要求立即处理的，见 :meth:`promote`）。原地修改不删行重插，消息身份因此从排队到并入始终不变。

只负责入队、按序读取、原地更新（含让位标记）、撤回与确认删除，不承载投递策略、输入解析或交接规则。唤醒器与待重判
集合是进程内状态，不跨重启；重启后调用方水位从零开始，首次等待直接读到遗留的全部行。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy import event
from sqlalchemy.orm import Session as DbSession

from app.config.logging.logger import log
from app.core.workflow.message_queue.session_message import SessionMessage
from app.storage.crud.session_message_crud import SessionMessageCrud
from app.storage.store_engines import main_db_factory


@dataclass(frozen=True)
class QueueRead:
    """一次等待读到的结果：``fresh`` 是调用方还没判定过的消息（水位之后新入队的与待重判的，按入队顺序），
    ``watermark`` 是读到的最大行 id，调用方下次等待时传回。不交出整个队列：读到之后、调用方判定之前队列
    可能已变，判定要在调用方的闸门内重读（见 ``SessionActor._advance``）。"""

    fresh: list[SessionMessage]
    watermark: int


# 一个等待方：所在事件循环与它的唤醒事件。put 可能在工作线程执行，只能经
# ``loop.call_soon_threadsafe`` 置位 asyncio 事件。
_Waiter = tuple[asyncio.AbstractEventLoop, asyncio.Event]


class SessionMessageQueue:
    def __init__(self) -> None:
        self._messages = SessionMessageCrud()
        # session_id → 正在 ``pending`` 里挂起的等待方；任意线程读写，由 ``_lock`` 保护。
        self._waiters: dict[int, set[_Waiter]] = {}
        # session_id → 要让等待方重新判定的行 id；由 ``_lock`` 保护，等待方读走即清。
        self._changed: dict[int, set[int]] = {}
        self._lock = threading.Lock()

    def put(self, session_id: int, message: SessionMessage, *, db: DbSession | None = None) -> int:
        """持久化一条消息并唤醒该 Session 的等待方，返回消息身份；任意线程可调。

        传入 ``db`` 时加入调用方事务，唤醒推迟到该事务提交之后：提交前唤醒的话，等待方读不到这一行
        又会睡回去，这次唤醒就丢了；事务回滚则不唤醒。
        """
        if db is not None:
            [message_id] = self._messages.append(db, [(session_id, message)])
            event.listen(db, "after_commit", lambda _s: self._notify(session_id), once=True)
        else:
            with main_db_factory().begin() as own:
                [message_id] = self._messages.append(own, [(session_id, message)])
            self._notify(session_id)
        log.info(
            "session_message_enqueued",
            extra={
                "msg": "消息已持久化，等待投递",
                "data": {
                    "session_id": session_id,
                    "message_id": message_id,
                    "kind": message.kind,
                    "delivery": message.delivery,
                },
            },
        )
        return message_id

    async def wait_unhandled(self, session_id: int, after_id: int = 0) -> QueueRead:
        """读出该 Session 里调用方还没判定过的消息（id 大于 ``after_id`` 的新行，或待重判的行）；没有时
        挂起，直到 ``put`` / ``promote`` 唤醒。

        不删除返回的消息（见模块 docstring）。先登记等待方、每轮先清唤醒标记、再取走待重判集合、最后读库：
        修改在提交之后才登记并唤醒，取走时库里一定已是修改后的行；读库之后到达的 ``put`` / ``promote``
        一定会置位标记，不会丢唤醒；多次写入合并为一次读库。待重判集合只交给一个等待方——每个 Session
        只有它的 actor 在等。调用方取消时等待方随之注销。
        """
        waiter: _Waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.setdefault(session_id, set()).add(waiter)
        try:
            changed: set[int] = set()
            while True:
                waiter[1].clear()
                with self._lock:
                    changed |= self._changed.pop(session_id, set())
                messages = await asyncio.to_thread(self.pending_nowait, session_id)
                fresh = [
                    message
                    for message in messages
                    if cast(int, message.id) > after_id or message.id in changed
                ]
                if fresh:
                    watermark = max(after_id, *(cast(int, message.id) for message in messages))
                    return QueueRead(fresh=fresh, watermark=watermark)
                await waiter[1].wait()
        finally:
            with self._lock:
                waiters = self._waiters.get(session_id)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        del self._waiters[session_id]

    def pending_nowait(self, session_id: int, *, db: DbSession | None = None) -> list[SessionMessage]:
        """不阻塞地按入队顺序读取该 Session 全部未确认的消息（可能为空）；传入 ``db`` 时在调用方
        事务内读取。"""
        if db is not None:
            return self._messages.list_pending(db, session_id)
        with main_db_factory()() as db:
            return self._messages.list_pending(db, session_id)

    def promote(self, db: DbSession, session_id: int, message_id: int) -> SessionMessage | None:
        """在调用方事务内表达「用户要这条输入现在就被处理」（立即发送）：仍在队列里的用户输入原地置为
        ``immediate``，返回改后的行；已并入、不是用户输入时返回 ``None``，由调用方定语义。

        已是 ``immediate`` 的插话同样登记：它可能早被判定过、却没被消费（投递后 Run 被停止或失败、模型
        不可用开不了 Run），水位已越过它，不登记就再也没人判定它。身份不变：不删行重插。与
        ``prepare_input`` 的确认并发时由这一条条件更新裁决。提交后登记为待重判并唤醒等待方，调度据此像对
        新消息一样判定（驱动中并入、挂起时唤醒、空闲时开新 Run）；事务回滚则不登记。
        """
        promoted = self._messages.promote_returning(db, session_id, message_id)
        if promoted is not None:
            event.listen(db, "after_commit", lambda _s: self._mark_changed(session_id, message_id), once=True)
        return promoted

    def withdraw(self, db: DbSession, session_id: int, message_id: int) -> SessionMessage | None:
        """在调用方事务内撤回一条仍在队列中的消息，返回撤回前的内容（带 ``interrupted_work``）。

        已被并入（行已删除）时返回 ``None``，由调用方定语义。与 ``prepare_input`` 的确认并发时由这一次
        条件删除裁决。只管事实：驱动期间 mailbox 里的副本由投递方一并拿掉（``SessionActor.withdraw_input``），
        这里不碰。不唤醒等待方：撤回不会产生需要推进的新消息。
        """
        return self._messages.delete_one_returning(db, session_id, message_id)

    def mark_interrupted_work(self, session_id: int, message_ids: list[int]) -> bool:
        """在途工作要为这些用户输入让位前调用：把仍在队列中的记为 ``interrupted_work``，返回是否至少有
        一条仍在，即这次让位是否成立；任意线程可调，自带事务。

        让位（同批调用不再执行、审批撤销、提问作废）不可撤回，判定与标记必须是同一笔条件更新：判定方多在
        Session 闸门外读 mailbox 副本，读到之后用户可能已删除这条输入——行不在就不让位。标记成功之后再
        撤回的，撤回方据此告诉模型（见 ``SessionActor.withdraw_input``）。重复标记无副作用。
        """
        with main_db_factory().begin() as db:
            marked = self._messages.mark_interrupted_work(db, session_id, message_ids)
        return bool(marked)

    def withdraw_matching(
        self, db: DbSession, session_id: int, matches: Callable[[SessionMessage], bool]
    ) -> list[SessionMessage]:
        """在调用方事务内撤回该 Session 队列中命中 ``matches`` 的全部消息（撤回回合时），返回
        撤回的行。哪些行属于这一回合由调用方判定，本层只按条件删除；不唤醒等待方。"""
        targets = [
            cast(int, message.id)
            for message in self._messages.list_pending(db, session_id)
            if matches(message)
        ]
        if not targets:
            return []
        return list(self._messages.delete_returning(db, targets).values())

    def ack(self, db: DbSession, ids: list[int]) -> dict[int, SessionMessage]:
        """在调用方事务内确认（删除）已并入 context 的行，返回实际删除的行内容（按 id）。

        与 context 写入同一事务提交，消息不会丢也不会被并入两次；不在返回值里的 id 已被别的
        执行确认过，调用方应跳过对应消息。调用方应写入返回的内容而不是 mailbox 里的副本。
        """
        return self._messages.delete_returning(db, ids)

    def _mark_changed(self, session_id: int, message_id: int) -> None:
        """登记一行待等待方重新判定，并唤醒它们；在提交之后调用。"""
        with self._lock:
            self._changed.setdefault(session_id, set()).add(message_id)
        self._notify(session_id)

    def _notify(self, session_id: int) -> None:
        """唤醒该 Session 的全部等待方；在提交之后调用，等待方醒来一定读得到新行。"""
        with self._lock:
            waiters = list(self._waiters.get(session_id, ()))
        for loop, wakeup in waiters:
            loop.call_soon_threadsafe(wakeup.set)
