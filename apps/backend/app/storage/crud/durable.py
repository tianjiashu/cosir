"""Durable run CRUD。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from sqlalchemy import asc, select, update
from sqlalchemy.exc import IntegrityError

from app.core.runs.records import ResumeCommandRecord, RunRecord
from app.storage.database import create_session_factory
from app.storage.model.durable import DurableRunModel, ResumeCommandModel
from app.storage.schema import initialize_app_schema


class DurableRunStore:
    """读写可恢复运行状态和恢复命令。"""

    def __init__(self, database_path: Path) -> None:
        """初始化运行状态仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果 schema 初始化失败。

        副作用:
            初始化主库 schema。
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create_for_task(self, task_id: str, status: str, thread_id: Optional[str] = None) -> RunRecord:
        """为任务创建或返回已有运行记录。"""

        existing = self.get_by_task(task_id)
        if existing is not None:
            return existing
        now = _utc_now()
        run = RunRecord(str(uuid4()), task_id, thread_id or str(uuid4()), status, None, None, None, None, None, now, now)
        with self._session_factory.begin() as session:
            session.add(_run_model(run))
        return run

    def get(self, run_id: str) -> RunRecord:
        """按 run_id 返回运行记录。"""

        with self._session_factory() as session:
            row = session.get(DurableRunModel, run_id)
        if row is None:
            raise KeyError(run_id)
        return _run_from_model(row)

    def get_by_task(self, task_id: str) -> Optional[RunRecord]:
        """按 task_id 返回运行记录。"""

        with self._session_factory() as session:
            row = session.execute(select(DurableRunModel).where(DurableRunModel.task_id == task_id)).scalar_one_or_none()
        return _run_from_model(row) if row is not None else None

    def mark_status(
        self,
        run_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
    ) -> RunRecord:
        """更新运行状态。"""

        with self._session_factory.begin() as session:
            result = session.execute(
                update(DurableRunModel)
                .where(DurableRunModel.run_id == run_id)
                .values(
                    status=status,
                    wait_reason=wait_reason,
                    active_step_id=active_step_id,
                    active_wait_id=active_wait_id,
                    interruption_reason=interruption_reason,
                    updated_at=_to_text(_utc_now()),
                )
            )
        if result.rowcount != 1:
            raise KeyError(run_id)
        return self.get(run_id)

    def list_runs_by_statuses(self, statuses: Sequence[str]) -> List[RunRecord]:
        """按状态集合列出运行记录。"""

        with self._session_factory() as session:
            rows = session.execute(
                select(DurableRunModel)
                .where(DurableRunModel.status.in_(tuple(statuses)))
                .order_by(asc(DurableRunModel.updated_at))
            ).scalars().all()
        return [_run_from_model(row) for row in rows]

    def create_resume_command(self, run_id: str, action: str, payload: Dict[str, Any], idempotency_key: str, status: str) -> ResumeCommandRecord:
        """创建幂等恢复命令。"""

        self.get(run_id)
        existing = self.get_resume_command_by_key(idempotency_key)
        if existing is not None:
            return existing
        command = ResumeCommandRecord(str(uuid4()), run_id, action, payload, idempotency_key, status, _utc_now(), None)
        try:
            with self._session_factory.begin() as session:
                session.add(_resume_model(command))
        except IntegrityError:
            concurrent = self.get_resume_command_by_key(idempotency_key)
            if concurrent is not None:
                return concurrent
            raise
        return command

    def get_resume_command_by_key(self, idempotency_key: str) -> Optional[ResumeCommandRecord]:
        """按幂等键查询恢复命令。"""

        with self._session_factory() as session:
            row = session.execute(select(ResumeCommandModel).where(ResumeCommandModel.idempotency_key == idempotency_key)).scalar_one_or_none()
        return _resume_from_model(row) if row is not None else None

    def claim_resume_commands(
        self,
        current_status: str,
        next_status: str,
        run_id: Optional[str] = None,
        actions: Optional[Sequence[str]] = None,
    ) -> List[ResumeCommandRecord]:
        """原子领取匹配状态的恢复命令。"""

        with self._session_factory.begin() as session:
            statement = select(ResumeCommandModel).where(ResumeCommandModel.status == current_status)
            if run_id is not None:
                statement = statement.where(ResumeCommandModel.run_id == run_id)
            if actions:
                statement = statement.where(ResumeCommandModel.action.in_(actions))
            rows = session.execute(statement.order_by(asc(ResumeCommandModel.created_at))).scalars().all()
            claimed: list[ResumeCommandRecord] = []
            for row in rows:
                result = session.execute(
                    update(ResumeCommandModel)
                    .where(ResumeCommandModel.command_id == row.command_id, ResumeCommandModel.status == current_status)
                    .values(status=next_status)
                )
                if result.rowcount == 1:
                    claimed.append(_resume_from_model(row))
        return claimed

    def update_resume_commands_status(
        self,
        current_status: str,
        next_status: str,
        run_id: Optional[str] = None,
        only_unapplied: bool = False,
    ) -> int:
        """批量更新匹配状态的恢复命令。"""

        statement = update(ResumeCommandModel).where(ResumeCommandModel.status == current_status).values(status=next_status)
        if only_unapplied:
            statement = statement.where(ResumeCommandModel.applied_at.is_(None))
        if run_id is not None:
            statement = statement.where(ResumeCommandModel.run_id == run_id)
        with self._session_factory.begin() as session:
            result = session.execute(statement)
        return result.rowcount or 0

    def update_resume_command_status(
        self,
        command_id: str,
        current_status: str,
        next_status: str,
        applied_at: Optional[datetime] = None,
    ) -> ResumeCommandRecord:
        """更新单条恢复命令状态。"""

        with self._session_factory.begin() as session:
            result = session.execute(
                update(ResumeCommandModel)
                .where(ResumeCommandModel.command_id == command_id, ResumeCommandModel.status == current_status)
                .values(status=next_status, applied_at=_to_text(applied_at) if applied_at else None)
            )
        if result.rowcount != 1:
            raise KeyError(command_id)
        return self._get_resume_command(command_id)

    def _get_resume_command(self, command_id: str) -> ResumeCommandRecord:
        """按主键读取恢复命令内部实现。"""

        with self._session_factory() as session:
            row = session.get(ResumeCommandModel, command_id)
        if row is None:
            raise KeyError(command_id)
        return _resume_from_model(row)


def _utc_now() -> datetime:
    """返回当前 UTC 时间。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 序列化为 ISO-8601 字符串。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """从 ISO-8601 字符串解析 datetime。"""

    return datetime.fromisoformat(value)


def _run_model(run: RunRecord) -> DurableRunModel:
    """将运行记录转换为 model。"""

    return DurableRunModel(
        run_id=run.run_id,
        task_id=run.task_id,
        thread_id=run.thread_id,
        status=run.status,
        wait_reason=run.wait_reason,
        active_step_id=run.active_step_id,
        active_wait_id=run.active_wait_id,
        last_checkpoint_id=run.last_checkpoint_id,
        interruption_reason=run.interruption_reason,
        created_at=_to_text(run.created_at),
        updated_at=_to_text(run.updated_at),
    )


def _run_from_model(row: DurableRunModel) -> RunRecord:
    """将运行 model 转换为记录。"""

    return RunRecord(row.run_id, row.task_id, row.thread_id, row.status, row.wait_reason, row.active_step_id, row.active_wait_id, row.last_checkpoint_id, row.interruption_reason, _from_text(row.created_at), _from_text(row.updated_at))


def _resume_model(command: ResumeCommandRecord) -> ResumeCommandModel:
    """将恢复命令记录转换为 model。"""

    return ResumeCommandModel(command_id=command.command_id, run_id=command.run_id, action=command.action, payload_json=json.dumps(command.payload, ensure_ascii=False, sort_keys=True), idempotency_key=command.idempotency_key, status=command.status, created_at=_to_text(command.created_at), applied_at=_to_text(command.applied_at) if command.applied_at else None)


def _resume_from_model(row: ResumeCommandModel) -> ResumeCommandRecord:
    """将恢复命令 model 转换为记录。"""

    return ResumeCommandRecord(row.command_id, row.run_id, row.action, json.loads(row.payload_json), row.idempotency_key, row.status, _from_text(row.created_at), _from_text(row.applied_at) if row.applied_at else None)
