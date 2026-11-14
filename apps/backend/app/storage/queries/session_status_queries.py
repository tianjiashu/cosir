"""Session 状态的只读投影：当前 Run 的状态（经当前 Run 指针）与「等你操作」的调用数。

不按创建时间猜测当前 Run。两项是正交的事实：Run 怎么走（唤醒、落定、停止）不影响有没有事在等你，
等你操作的事实源是 HITL 等待占位行（规则见 ``SessionContextCrud.awaiting_user``）。
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.model.run_model import RunModel
from app.storage.model.session_context_model import SessionContextModel
from app.storage.model.session_model import SessionModel


@dataclass(frozen=True)
class SessionStatus:
    """一个 Session 的状态：执行状态（当前 Run 的 ``status``，无 Run 时为 None）与等你操作的调用数。"""

    execution_status: str | None
    awaiting_user_calls: int


class SessionStatusQueries:
    """两项在同一个读事务里读取，避免指针、状态与等待数来自不同读取时刻。"""

    def __init__(self, db_factory: sessionmaker[DbSession]) -> None:
        self._db_factory = db_factory

    _EXECUTION = select(SessionModel.id, RunModel.status).outerjoin(
        RunModel, SessionModel.current_run_id == RunModel.id
    )
    # 部分索引只收录 pending 行：整表对账只读当下在等的那几行。
    _AWAITING = (
        select(SessionContextModel.session_id, func.count())
        .where(SessionContextCrud.awaiting_user())
        .group_by(SessionContextModel.session_id)
    )

    def status(self, session_id: int) -> SessionStatus:
        """单个 Session 的状态；Session 不存在时抛 ``KeyError``。"""
        with self._db_factory() as db:
            row = db.execute(self._EXECUTION.where(SessionModel.id == session_id)).one_or_none()
            if row is None:
                raise KeyError(session_id)
            awaiting = db.execute(
                self._AWAITING.where(SessionContextModel.session_id == session_id)
            ).one_or_none()
        return SessionStatus(row.status, awaiting[1] if awaiting else 0)

    def list_statuses(self) -> dict[int, SessionStatus]:
        """全部 Session（含委派子会话）的状态，供状态推送的整表帧与周期对账。"""
        with self._db_factory() as db:
            executions = db.execute(self._EXECUTION).tuples().all()
            awaiting = dict(db.execute(self._AWAITING).tuples().all())
        return {
            session_id: SessionStatus(status, awaiting.get(session_id, 0)) for session_id, status in executions
        }
