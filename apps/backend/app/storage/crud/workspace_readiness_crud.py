"""Workspace readiness snapshot persistence."""

from sqlalchemy import select

from app.models.workspace_readiness import WorkspaceReadiness
from app.storage.model.workspace_readiness_model import WorkspaceReadinessModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text


class WorkspaceReadinessCrud:
    """读写每个 workspace 的单行 readiness 快照。"""

    def __init__(self) -> None:
        """绑定已初始化的主库 session 工厂。"""
        self._session_factory = main_session_factory()

    def get(self, workspace_id: int) -> WorkspaceReadiness | None:
        """读取 workspace 当前快照，不存在时返回 ``None``。"""
        with self._session_factory() as session:
            row = session.execute(
                select(WorkspaceReadinessModel).where(
                    WorkspaceReadinessModel.workspace_id == workspace_id
                )
            ).scalar_one_or_none()
        return None if row is None else self._to_record(row)

    def upsert(self, workspace_id: int, readiness: WorkspaceReadiness) -> WorkspaceReadiness:
        """以原子单行更新写入最新状态并递增 revision。"""
        with self._session_factory.begin() as session:
            row = session.execute(
                select(WorkspaceReadinessModel).where(
                    WorkspaceReadinessModel.workspace_id == workspace_id
                )
            ).scalar_one_or_none()
            if row is None:
                row = WorkspaceReadinessModel(
                    workspace_id=workspace_id,
                    status=readiness.state,
                    reason=readiness.degraded_reason,
                    action_taken=readiness.action_taken,
                    files_changed=readiness.files_changed,
                    duration_ms=readiness.duration_ms,
                    revision=1,
                )
                session.add(row)
            else:
                row.status = readiness.state
                row.reason = readiness.degraded_reason
                row.action_taken = readiness.action_taken
                row.files_changed = readiness.files_changed
                row.duration_ms = readiness.duration_ms
                row.revision += 1
            session.flush()
            return WorkspaceReadiness(
                ready=readiness.ready,
                state=row.status,
                action_taken=row.action_taken,
                files_changed=row.files_changed,
                duration_ms=row.duration_ms,
                degraded_reason=row.reason,
                workspace_id=row.workspace_id,
                revision=row.revision,
                updated_at=from_text(row.updated_at),
            )

    @staticmethod
    def _to_record(row: WorkspaceReadinessModel) -> WorkspaceReadiness:
        """将 ORM 快照转换为领域值对象。"""
        return WorkspaceReadiness(
            ready=row.status == "ready",
            state=row.status,
            action_taken=row.action_taken,
            files_changed=row.files_changed,
            duration_ms=row.duration_ms,
            degraded_reason=row.reason,
            workspace_id=row.workspace_id,
            revision=row.revision,
            updated_at=from_text(row.updated_at),
        )
