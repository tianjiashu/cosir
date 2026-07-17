"""TaskStoreMixin implementation for SQLiteTaskStore."""

from app.storage.crud.task_common import *


class TaskStoreMixin:
    """SQLiteTaskStore TaskStoreMixin responsibilities."""

    def create_task(
        self,
        input_text: str,
        status: str,
        agent_id: str = "developer",
        workspace_id: Optional[str] = None,
    ) -> TaskRecord:
        """在工作区下创建任务容器与首个轮次记录。

        参数:
            input_text: 用户提交的纯文本任务。
            status: 新任务初始状态。
            agent_id: 负责执行任务的 Agent 标识符。
            workspace_id: 可选工作区标识；省略时创建默认工作区。

        返回:
            新创建的任务记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果数据库写入失败。

        副作用:
            向主库写入任务与首个轮次记录。
        """

        now = _utc_now()
        resolved_workspace_id = workspace_id or self._ensure_default_workspace(now)
        title = _preview(input_text)
        turn_id = str(uuid4())
        task = TaskRecord(
            task_id=str(uuid4()),
            workspace_id=resolved_workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            title=title,
            last_message_preview=title,
            latest_turn_id=turn_id,
            status=status,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory.begin() as session:
            if session.get(WorkspaceModel, resolved_workspace_id) is None:
                raise KeyError(resolved_workspace_id)
            session.add(
                TaskModel(
                    task_id=task.task_id,
                    workspace_id=task.workspace_id,
                    agent_id=task.agent_id,
                    input_text=task.input_text,
                    title=task.title,
                    last_message_preview=task.last_message_preview,
                    latest_turn_id=task.latest_turn_id,
                    status=task.status,
                    created_at=_to_text(task.created_at),
                    updated_at=_to_text(task.updated_at),
                )
            )
            session.add(
                TurnModel(
                    turn_id=turn_id,
                    task_id=task.task_id,
                    input_text=input_text,
                    status=status,
                    created_at=_to_text(now),
                    updated_at=_to_text(now),
                )
            )
        return task

    def list_tasks_for_workspace(self, workspace_id: str) -> List[TaskRecord]:
        """列出指定工作区下的任务容器。

        参数:
            workspace_id: 待查询的工作区标识符。

        返回:
            任务记录列表，按更新时间倒序排列。

        异常:
            KeyError: 如果工作区不存在。

        副作用:
            无。
        """

        self.get_workspace(workspace_id)
        with self._session_factory() as session:
            rows = session.execute(
                select(TaskModel)
                .where(TaskModel.workspace_id == workspace_id)
                .order_by(TaskModel.updated_at.desc(), TaskModel.task_id.desc())
            ).scalars().all()
        return [_task_from_model(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        """按标识符返回一个任务。

        参数:
            task_id: 待获取的任务标识符。

        返回:
            匹配的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        with self._session_factory() as session:
            row = session.get(TaskModel, task_id)
        if row is None:
            raise KeyError(task_id)
        return _task_from_model(row)

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        """更新任务状态并返回更新后的任务。

        参数:
            task_id: 待更新的任务标识符。
            status: 新的任务状态。

        返回:
            更新后的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改主库中的任务状态。
        """

        self.get_task(task_id)
        with self._session_factory.begin() as session:
            session.execute(update(TaskModel).where(TaskModel.task_id == task_id).values(status=status, updated_at=_to_text(_utc_now())))
        return self.get_task(task_id)

    def has_status(self, task_id: str, status: str) -> bool:
        """返回任务当前是否具有某状态值。

        参数:
            task_id: 待检查的任务标识符。
            status: 需要与所存储任务状态比较的状态值。

        返回:
            当任务状态等于所给状态时为 True。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self.get_task(task_id).status == status
