"""WorkspaceStoreMixin implementation for SQLiteTaskStore."""

import logging

from app.storage.crud.task_common import *

_LOGGER = logging.getLogger("coding_agent.backend")


class WorkspaceStoreMixin:
    """SQLiteTaskStore WorkspaceStoreMixin responsibilities."""

    def _ensure_default_workspace(self, now: datetime) -> str:
        """确保兼容旧任务入口的默认工作区存在。

        参数:
            now: 用于写入创建与更新时间的 UTC 时间。

        返回:
            默认工作区标识符。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果数据库写入失败。

        副作用:
            在缺失时写入一条默认 workspace 记录。
        """

        workspace_id = "default-workspace"
        with self._session_factory.begin() as session:
            if session.get(WorkspaceModel, workspace_id) is None:
                session.add(
                    WorkspaceModel(
                        workspace_id=workspace_id,
                        name="Default Workspace",
                        root_path=".",
                        created_at=_to_text(now),
                        updated_at=_to_text(now),
                    )
                )
        return workspace_id

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """创建一个本地工作区记录。

        参数:
            name: 用户可读的工作区名称。
            root_path: 工作区的本地文件系统路径。

        返回:
            已创建的工作区记录。

        异常:
            ValueError: 如果名称或路径为空白。
            sqlalchemy.exc.SQLAlchemyError: 如果数据库写入失败。

        副作用:
            向主库写入一行工作区记录。
        """

        if not name.strip():
            raise ValueError("workspace name must not be blank")
        if not root_path.strip():
            raise ValueError("workspace root_path must not be blank")
        now = _utc_now()
        workspace = WorkspaceRecord(str(uuid4()), name.strip(), root_path.strip(), now, now)
        with self._session_factory.begin() as session:
            session.add(
                WorkspaceModel(
                    workspace_id=workspace.workspace_id,
                    name=workspace.name,
                    root_path=workspace.root_path,
                    created_at=_to_text(workspace.created_at),
                    updated_at=_to_text(workspace.updated_at),
                )
            )
        return workspace

    def list_workspaces(self) -> List[WorkspaceRecord]:
        """按创建时间列出所有工作区。

        参数:
            无。

        返回:
            工作区记录列表。

        异常:
            无。

        副作用:
            无。
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(WorkspaceModel).order_by(asc(WorkspaceModel.created_at), asc(WorkspaceModel.workspace_id))
            ).scalars().all()
        return [_workspace_from_model(row) for row in rows]

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        """按标识符返回一个工作区。

        参数:
            workspace_id: 待获取的工作区标识符。

        返回:
            匹配的工作区记录。

        异常:
            KeyError: 如果工作区不存在。

        副作用:
            无。
        """

        with self._session_factory() as session:
            row = session.get(WorkspaceModel, workspace_id)
        if row is None:
            raise KeyError(workspace_id)
        return _workspace_from_model(row)

    def delete_workspace(self, workspace_id: str) -> None:
        """删除工作区并级联删除其下任务运行记录。

        参数:
            workspace_id: 待删除的工作区标识符。

        返回:
            无。

        异常:
            KeyError: 如果工作区不存在。

        副作用:
            删除 workspace、tasks、turns、steps、events 和 checkpoints 表中的关联记录。
        """

        self.get_workspace(workspace_id)
        task_ids: list[str] = []
        turn_ids: list[str] = []
        try:
            with self._session_factory.begin() as session:
                task_ids = [
                    row[0]
                    for row in session.execute(select(TaskModel.task_id).where(TaskModel.workspace_id == workspace_id)).all()
                ]
                if task_ids:
                    turn_ids = [
                        row[0]
                        for row in session.execute(select(TurnModel.turn_id).where(TurnModel.task_id.in_(task_ids))).all()
                    ]
                    # 注意：events / steps 外键指向 turns，必须先删除；turns 外键指向 tasks，所以
                    # events / checkpoints / steps 都要在 turns 之前删除，否则 foreign_keys=ON 会触发冲突。
                    session.execute(delete(EventModel).where(EventModel.task_id.in_(task_ids)))
                    session.execute(delete(CheckpointModel).where(CheckpointModel.task_id.in_(task_ids)))
                    if turn_ids:
                        session.execute(delete(StepModel).where(StepModel.turn_id.in_(turn_ids)))
                        session.execute(delete(TurnModel).where(TurnModel.turn_id.in_(turn_ids)))
                    session.execute(delete(TaskModel).where(TaskModel.task_id.in_(task_ids)))
                session.execute(delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id))
        except Exception:
            _LOGGER.exception(
                "workspace_delete_failed",
                extra={
                    "msg": f"删除工作区及下游记录写入数据库失败，workspace_id={workspace_id}",
                    "data": {"workspace_id": workspace_id, "operation": "delete_workspace"},
                },
            )
            raise
        _LOGGER.info(
            "workspace_cascade_deleted",
            extra={
                "msg": f"工作区及下游任务运行记录已删除，workspace_id={workspace_id}",
                "data": {"workspace_id": workspace_id, "task_count": len(task_ids), "turn_count": len(turn_ids)},
            },
        )
