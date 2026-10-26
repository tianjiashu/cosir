"""Task 外部消息队列：持久化、按入队顺序先进先出，是待并入消息的唯一事实源。

只负责入队、按序读取、原地更新与确认删除，不承载投递策略、输入解析或交接规则——投递时机由
消息自身的 ``delivery`` 决定，由 ``TaskActors`` 执行。投递不删行；``prepare_input`` 写
context 时在同一事务内 :meth:`TaskMessageQueue.ack` 删除，执行结束、取消或进程重启时 mailbox 随之
丢弃，未确认的行仍在这里，交接或下次投递时重新送达。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.models.task_message import TaskMessage
from app.storage.crud.task_message_crud import TaskMessageCrud
from app.storage.store_engines import main_session_factory


class TaskMessageQueue:
    def __init__(self) -> None:
        self._messages = TaskMessageCrud()

    def put(self, task_id: int, message: TaskMessage, *, session: Session | None = None) -> int:
        """持久化一条消息并返回其身份；传入 ``session`` 时加入调用方事务（编辑的移出与入队）。"""
        if session is not None:
            [message_id] = self._messages.append(session, [(task_id, message)])
        else:
            with main_session_factory().begin() as own:
                [message_id] = self._messages.append(own, [(task_id, message)])
        log.info(
            "task_message_enqueued",
            extra={
                "msg": "消息已持久化，等待投递",
                "data": {
                    "task_id": task_id,
                    "message_id": message_id,
                    "kind": message.body.kind,
                    "delivery": message.delivery,
                },
            },
        )
        return message_id

    def pending(self, task_id: int) -> list[TaskMessage]:
        with main_session_factory()() as session:
            return self._messages.list_pending(session, task_id)

    def update(self, task_id: int, message_id: int, message: TaskMessage) -> bool:
        """原地更新仍在队列中的消息正文，身份与属性不变；已被并入（行已删除）时返回 ``False``。

        与 ``prepare_input`` 的确认并发时由这一次条件更新裁决，不另加前置检查。
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
