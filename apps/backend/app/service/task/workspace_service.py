"""Workspace orchestration service.

单一职责：编排工作区的创建与级联删除——删除工作区时级联清理
其下的所有任务、轮次、消息轨迹与运行时事件（执行态已收敛到 Turn，不再有 durable run 级联）。

职责边界：
- 负责：工作区创建、列表查询、级联删除
  （task + turn + turn_message + workspace_payload）。
- 不负责：直接 SQL 操作（委托给 ``WorkspaceCrud``/``TaskCrud``/
  ``TurnCrud``/``TurnMessageCrud``/``RuntimeEventCrud``）。
"""

from app.config.logging.logger import log
from app.models import WorkspaceRecord
from app.service import depends as service_depends


class WorkspaceService:
    """Orchestrate workspace creation, queries, and cascade deletion."""

    def __init__(self) -> None:
        """初始化工作区 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._task = service_depends.get_task_crud()
        self._turn = service_depends.get_turn_crud()
        self._workspace = service_depends.get_workspace_crud()
        self._runtime_event = service_depends.get_runtime_event_crud()
        self._turn_message = service_depends.get_turn_message_crud()
        self._delegation = service_depends.get_delegation_crud()

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        return self._workspace.create(name, root_path)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        return self._workspace.list_all()

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        return self._workspace.get(workspace_id)

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and cascade its tasks/turns/messages/events/delegations.

        级联删除前先收集该工作区下的任务与轮次标识，按
        ``runtime_events -> turn_messages -> turns -> tasks -> delegations -> workspace``
        顺序清理，避免外键 / 孤儿数据。委派子 Agent 的 ``delegations`` 行以 ``task_id``
        关联，本方法直接走 task crud（不经 ``TaskService.delete_task``），因此须显式清理
        delegation，否则删除后成为无法追溯的孤儿记录。
        删除是高风险操作，保留 start / complete 审计日志。

        参数:
            workspace_id: 待删除的工作区标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``runtime_events`` / ``turn_messages`` / ``turns`` / ``tasks`` /
            ``delegations`` / ``workspaces`` 表删除该工作区相关数据。
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
        # 委派子 Agent 的 delegation 记录以 task_id 关联，workspace 级联删除直接走
        # task crud 而非 TaskService.delete_task，因此须在此显式清理，避免孤儿记录。
        deleted_delegations = self._delegation.delete_by_task_ids(task_ids)
        self._workspace.delete(workspace_id)
        log.info(
            "workspace_deleted",
            extra={
                "msg": "workspace deleted",
                "data": {
                    "workspace_id": workspace_id,
                    "task_ids": task_ids,
                    "deleted_delegations": deleted_delegations,
                },
            },
        )
