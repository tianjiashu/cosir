"""Task orchestration service.

单一职责：编排任务创建（含初始轮次创建）、状态查询与更新。

职责边界：
- 负责：任务创建（同时创建首个轮次）、状态管理。
- 不负责：直接 SQL 操作（委托给 ``TaskCrud``/``TurnCrud``/``WorkspaceCrud``）。
"""

from typing import Optional
from uuid import uuid4

from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.models import TaskRecord
from app.utils.datetime_utils import preview, utc_now


class TaskService:
    """Orchestrate task creation, status management, and queries."""

    def __init__(
        self,
        task_crud: TaskCrud,
        turn_crud: TurnCrud,
        workspace_crud: WorkspaceCrud,
    ) -> None:
        self._task = task_crud
        self._turn = turn_crud
        self._workspace = workspace_crud

    def create_task(
        self,
        input_text: str,
        status: str,
        agent_id: str = "developer",
        workspace_id: Optional[str] = None,
    ) -> TaskRecord:
        """Create a task and its first turn (atomic)."""
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        now = utc_now()
        resolved_workspace_id = workspace_id or self._workspace.ensure_default(now)
        title = preview(input_text)
        turn_id = str(uuid4())
        task = self._task.create(
            task_id=str(uuid4()),
            workspace_id=resolved_workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            title=title,
            last_message_preview=title,
            latest_turn_id=turn_id,
            status=status,
        )
        self._turn.create(task_id=task.task_id, input_text=input_text, status=status)
        return task

    def get_task(self, task_id: str) -> TaskRecord:
        return self._task.get(task_id)

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        return self._task.update_status(task_id, status)

    def has_status(self, task_id: str, status: str) -> bool:
        return self._task.has_status(task_id, status)

    def list_tasks_for_workspace(self, workspace_id: str) -> list[TaskRecord]:
        return self._task.list_by_workspace(workspace_id)
