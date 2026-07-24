"""Workspace orchestration service.

单一职责：编排工作区的创建与级联删除——删除工作区时级联清理
其下的所有任务、轮次、消息轨迹与运行时事件（执行态已收敛到 Turn，不再有 durable run 级联）。

职责边界：
- 负责：工作区创建、列表查询、级联删除
  （task + turn + turn_message + runtime_event）。
- 不负责：直接 SQL 操作（委托给 ``WorkspaceCrud``/``TaskCrud``/
  ``TurnCrud``/``TurnMessageCrud``/``RuntimeEventCrud``）。
"""

from app.config.logging.logger import log
from app.models import WorkspaceRecord
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.crud.workspace_crud import WorkspaceCrud


class WorkspaceService:
    """Orchestrate workspace creation, queries, and cascade deletion."""

    def __init__(
        self,
        task_crud: TaskCrud,
        turn_crud: TurnCrud,
        workspace_crud: WorkspaceCrud,
        runtime_event_crud: RuntimeEventCrud,
        turn_message_crud: TurnMessageCrud,
    ) -> None:
        self._task = task_crud
        self._turn = turn_crud
        self._workspace = workspace_crud
        self._runtime_event = runtime_event_crud
        self._turn_message = turn_message_crud

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        return self._workspace.create(name, root_path)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        return self._workspace.list_all()

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        return self._workspace.get(workspace_id)

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and cascade its tasks, turns, messages and runtime events.

        级联删除前先收集该工作区下的任务与轮次标识，按
        ``runtime_events -> turn_messages -> turns -> tasks -> workspace`` 顺序清理，
        避免外键 / 孤儿数据。
        删除是高风险操作，保留 start / complete 审计日志。
        """

        task_ids = self._task.list_ids_by_workspace(workspace_id)
        log.info(
            "workspace_delete_start",
            extra={
                "msg": "workspace delete started",
                "data": {"workspace_id": workspace_id, "task_count": len(task_ids)},
            },
        )
        if task_ids:
            turn_ids = self._turn.list_ids_by_task_ids(task_ids)
            if turn_ids:
                # 先清理最细粒度的运行时事件与消息轨迹，再清理轮次，避免孤儿数据残留。
                self._runtime_event.delete_by_turn_ids(turn_ids)
                self._turn_message.delete_by_turn_ids(turn_ids)
                self._turn.delete_by_ids(turn_ids)
            self._task.delete_by_ids(task_ids)
        self._workspace.delete(workspace_id)
        log.info(
            "workspace_deleted",
            extra={
                "msg": "workspace deleted",
                "data": {"workspace_id": workspace_id, "task_ids": task_ids},
            },
        )
