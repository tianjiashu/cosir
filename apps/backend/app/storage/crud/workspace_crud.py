"""SQLite workspace CRUD — pure data access for the ``workspaces`` table.

单一职责：提供 ``workspaces`` 表的纯 CRUD 操作。

职责边界：
- 负责：workspace 单表读写、model↔record 转换。
- 不负责：跨表级联删除（由 ``WorkspaceService`` 编排）。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import asc, select
from sqlalchemy.orm import sessionmaker

from app.storage.model.workspace_model import WorkspaceModel
from app.service.task.records import WorkspaceRecord
from app.utils.datetime_utils import from_text, to_text



class WorkspaceCrud:
    """Pure CRUD for the ``workspaces`` table."""

    def __init__(self, session_factory: sessionmaker) -> None:
        """Initialize with a session factory."""
        self._session_factory = session_factory

    def ensure_default(self, now: datetime) -> str:
        """Ensure the default workspace exists."""

        workspace_id = "default-workspace"
        with self._session_factory.begin() as session:
            if session.get(WorkspaceModel, workspace_id) is None:
                session.add(
                    WorkspaceModel(
                        workspace_id=workspace_id,
                        name="Default Workspace",
                        root_path=".",
                        created_at=to_text(now),
                        updated_at=to_text(now),
                    )
                )
        return workspace_id

    def create(self, name: str, root_path: str) -> WorkspaceRecord:
        """Create a workspace record."""

        if not name.strip():
            raise ValueError("workspace name must not be blank")
        if not root_path.strip():
            raise ValueError("workspace root_path must not be blank")
        now = datetime.now()
        workspace = WorkspaceRecord(str(uuid4()), name.strip(), root_path.strip(), now, now)
        with self._session_factory.begin() as session:
            session.add(
                WorkspaceModel(
                    workspace_id=workspace.workspace_id,
                    name=workspace.name,
                    root_path=workspace.root_path,
                    created_at=to_text(workspace.created_at),
                    updated_at=to_text(workspace.updated_at),
                )
            )
        return workspace

    def list_all(self) -> list[WorkspaceRecord]:
        """List all workspaces."""

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(WorkspaceModel).order_by(
                        asc(WorkspaceModel.created_at), asc(WorkspaceModel.workspace_id)
                    )
                )
                .scalars()
                .all()
            )
        return [self._workspace_from_model(row) for row in rows]

    def get(self, workspace_id: str) -> WorkspaceRecord:
        """Return a workspace by identifier."""

        with self._session_factory() as session:
            row = session.get(WorkspaceModel, workspace_id)
        if row is None:
            raise KeyError(workspace_id)
        return self._workspace_from_model(row)

    def delete(self, workspace_id: str) -> None:
        """Delete a workspace record."""

        from sqlalchemy import delete

        with self._session_factory.begin() as session:
            session.execute(
                delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id)
            )

    def _workspace_from_model(self,row: WorkspaceModel) -> WorkspaceRecord:
        """Convert a workspace ORM model to a domain record."""
        return WorkspaceRecord(
            row.workspace_id,
            row.name,
            row.root_path,
            from_text(row.created_at),
            from_text(row.updated_at),
        )
