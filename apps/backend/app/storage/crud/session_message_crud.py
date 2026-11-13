"""外部消息队列的存取；事务、投递与确认语义由 ``SessionMessageQueue`` 决定。"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.core.workflow.message_queue.session_message import SessionMessage
from app.storage.model.session_message_model import SessionMessageModel


class SessionMessageCrud:
    def append(
            self, db: DbSession, rows: Iterable[tuple[int, SessionMessage]]
    ) -> list[int]:
        """按顺序追加 ``(session_id, message)``，返回行 id（即消息的稳定身份）。"""

        models = [
            SessionMessageModel(
                session_id=session_id,
                kind=message.kind,
                payload=message.body.model_dump(mode="json"),
                delivery=message.delivery,
                project_to_transport=message.project_to_transport,
            )
            for session_id, message in rows
        ]
        db.add_all(models)
        db.flush()
        return [model.id for model in models]

    def list_pending(self, db: DbSession, session_id: int) -> list[SessionMessage]:
        """按入队顺序列出待并入消息；读取边界按 ``kind`` 校验 payload。"""

        statement = (
            select(SessionMessageModel)
            .where(SessionMessageModel.session_id == session_id)
            .order_by(SessionMessageModel.id)
        )
        return [SessionMessage.of_model(row) for row in db.scalars(statement)]

    def list_session_ids_with_pending(
            self, db: DbSession, *, kinds: frozenset[str] | None = None
    ) -> list[int]:
        """返回外部队列里有待处理消息的 session id；``kinds`` 限定哪些消息算待处理。

        能推进 Run 的 kind 由 ``message_bodies.ADVANCING_KINDS`` 提供：通知这类不推进的消息
        仍会在队列里等下次并入，但不该让启动调度为它拉起 actor。
        """

        statement = select(SessionMessageModel.session_id).distinct()
        if kinds is not None:
            statement = statement.where(SessionMessageModel.kind.in_(kinds))
        return list(db.scalars(statement).all())

    def delete_one_returning(
            self, db: DbSession, session_id: int, message_id: int
    ) -> SessionMessage | None:
        """删除一条仍在队列中的消息并返回删除前的内容；已被确认删除或不属于该 Session 时返回 ``None``。"""

        row = db.scalar(
            delete(SessionMessageModel)
            .where(SessionMessageModel.id == message_id, SessionMessageModel.session_id == session_id)
            .returning(SessionMessageModel)
        )
        return SessionMessage.of_model(row) if row is not None else None

    def delete_returning(self, db: DbSession, ids: list[int]) -> dict[int, SessionMessage]:
        """删除并返回实际删除的行（按 id）；已被删除的行不在结果里，供调用方识别重复确认。

        返回删除那一刻的行内容，调用方以此为准，而不是投递时的副本。
        """

        if not ids:
            return {}
        result = db.scalars(
            delete(SessionMessageModel)
            .where(SessionMessageModel.id.in_(ids))
            .returning(SessionMessageModel)
        )
        return {row.id: SessionMessage.of_model(row) for row in result}
