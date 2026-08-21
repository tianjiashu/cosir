"""Workspace orchestration service.

单一职责：编排工作区的创建、列表查询与原子级联删除——删除工作区时在单个写锁事务内
清理其下所有任务树及全部子产物（轮次、消息轨迹、运行时事件、文件快照、委派记录）。

职责边界：
- 负责：工作区创建、列表查询、原子级联删除（委托给 ``CascadeDeleter``）。
- 不负责：直接 SQL 操作（委托给 ``WorkspaceCrud``/``CascadeDeleter``）。
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

        self._workspace = service_depends.get_workspace_crud()
        self._cascade_deleter = service_depends.get_cascade_deleter()

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """创建工作区记录。

        参数:
            name: 工作区名称。
            root_path: 工作区根目录的绝对路径。

        返回:
            已持久化的 ``WorkspaceRecord``。

        异常:
            ValueError: 当 ``name`` 或 ``root_path`` 去除首尾空白后为空时抛出。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``workspaces`` 表插入一行记录。
        """
        return self._workspace.create(name, root_path)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        """列出全部工作区。

        参数:
            无。

        返回:
            全部工作区记录列表；无数据时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._workspace.list_all()

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        """按标识取单个工作区。

        参数:
            workspace_id: 工作区标识。

        返回:
            对应的工作区记录。

        异常:
            KeyError: 如果指定工作区不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._workspace.get(workspace_id)

    def delete_workspace(self, workspace_id: str) -> None:
        """原子删除工作区并级联清理其下所有任务树及全部子产物。

        删除前先校验工作区存在（不存在则抛 ``KeyError``），再通过
        ``CascadeDeleter.delete_workspace`` 在单个 ``BEGIN IMMEDIATE`` 写锁事务内
        收集该工作区全部任务（含递归委派子任务），按
        ``turn_messages -> file_snapshots -> turns -> runtime_events -> delegations ->
        tasks -> workspace``
        顺序清理，保证原子性（全删或全不删）与并发安全（删除期间无并发写插入孤儿数据）。
        删除是高风险操作，保留 start / complete 审计日志。

        参数:
            workspace_id: 待删除的工作区标识。

        返回:
            无。

        异常:
            KeyError: 如果指定工作区不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``turn_messages`` / ``runtime_events`` / ``file_snapshots`` /
            ``turns`` / ``delegations`` / ``tasks`` / ``workspaces`` 表删除该工作区
            相关数据。
        """

        self._workspace.get(workspace_id)  # 存在性守卫，不存在抛 KeyError
        log.info(
            "workspace_delete_start",
            extra={
                "msg": "workspace delete started",
                "data": {"workspace_id": workspace_id},
            },
        )
        self._cascade_deleter.delete_workspace(workspace_id)
        log.info(
            "workspace_deleted",
            extra={"msg": "workspace deleted", "data": {"workspace_id": workspace_id}},
        )
