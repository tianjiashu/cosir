"""``terminal_sessions`` 单表 CRUD。"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.terminal_session_record import TerminalSessionRecord
from app.storage.model.terminal_session_model import TerminalSessionModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class TerminalSessionCrud:
    """只负责终端 session 元数据的单表读写。

    跨表归属校验、session 状态机和 worker 清理由 ``TerminalSessionService`` 负责。
    """

    def __init__(self) -> None:
        """绑定已初始化主库的 session factory。"""

        self._session_factory = main_session_factory()

    def create(self, record: TerminalSessionRecord) -> TerminalSessionRecord:
        """插入一条 session 元数据并返回持久化值对象。"""

        model = TerminalSessionModel(
            session_id=record.session_id,
            task_id=record.task_id,
            workspace_id=record.workspace_id,
            created_by_run_id=record.created_by_run_id,
            initial_cwd=record.initial_cwd,
            shell_kind=record.shell_kind,
            shell_executable=record.shell_executable,
            worker_instance_id=record.worker_instance_id,
            worker_pid=record.worker_pid,
            status=record.status,
            end_reason=record.end_reason,
            exit_code=record.exit_code,
            cols=record.cols,
            rows=record.rows,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
            last_activity_at=to_text(record.last_activity_at),
            ended_at=to_text(record.ended_at) if record.ended_at else None,
        )
        with self._session_factory.begin() as session:
            session.add(model)
            session.flush()
            return TerminalSessionRecord.from_model(model)

    def get(self, session_id: str) -> TerminalSessionRecord:
        """按公开 session id 查询记录，不存在时抛出 ``KeyError``。"""

        with self._session_factory() as session:
            row = session.scalar(
                select(TerminalSessionModel).where(TerminalSessionModel.session_id == session_id)
            )
        if row is None:
            raise KeyError(session_id)
        return TerminalSessionRecord.from_model(row)

    def update_runtime(
        self,
        session_id: str,
        *,
        status: str | None = None,
        end_reason: str | None = None,
        exit_code: int | None = None,
        worker_pid: int | None = None,
        cols: int | None = None,
        rows: int | None = None,
        ended_at: datetime | str | None = None,
        last_activity_at: str | None = None,
    ) -> TerminalSessionRecord:
        """更新 session 的运行元数据并返回最新记录。"""

        values: dict[str, object] = {"updated_at": to_text(utc_now())}
        for name, value in (
            ("status", status),
            ("end_reason", end_reason),
            ("exit_code", exit_code),
            ("worker_pid", worker_pid),
            ("cols", cols),
            ("rows", rows),
            ("ended_at", ended_at),
            ("last_activity_at", last_activity_at),
        ):
            if value is not None:
                values[name] = (
                    to_text(value) if name == "ended_at" and isinstance(value, datetime) else value
                )
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TerminalSessionModel)
                .where(TerminalSessionModel.session_id == session_id)
                .values(**values)
            )
            if result.rowcount != 1:
                raise KeyError(session_id)
        return self.get(session_id)

    def recover_active(self, reason: str = "backend_restart") -> list[str]:
        """原子收敛遗留 active session，并返回实际受影响的 session id。"""

        now = to_text(utc_now())
        with self._session_factory.begin() as session:
            rows = session.scalars(
                select(TerminalSessionModel.session_id).where(
                    TerminalSessionModel.status.in_(("starting", "running"))
                )
            ).all()
            if rows:
                session.execute(
                    update(TerminalSessionModel)
                    .where(TerminalSessionModel.status.in_(("starting", "running")))
                    .values(
                        status="interrupted",
                        end_reason=reason,
                        ended_at=now,
                        last_activity_at=now,
                        updated_at=now,
                    )
                )
        return list(rows)

    def delete_by_task_ids(self, task_ids: Iterable[int], session: Session | None = None) -> int:
        """删除指定 Task 的 session 元数据；worker 清理由 service 先完成。"""

        ids = list(task_ids)
        if not ids:
            return 0
        statement = delete(TerminalSessionModel).where(TerminalSessionModel.task_id.in_(ids))
        if session is not None:
            result = session.execute(statement)
            return int(result.rowcount or 0)
        with self._session_factory.begin() as owned_session:
            result = owned_session.execute(statement)
            return int(result.rowcount or 0)
