"""Workspace orchestration service.

单一职责：编排工作区的创建、列表查询、受闸门保护的 task 创建，以及 workspace 级联删除。
删除 workspace 时在一个 SQLite 写事务内清理任务树、全部子产物和 workspace 记录。

职责边界：
- 负责：工作区创建、列表查询、workspace/task 写闸门和级联删除编排（任务产物删除委托给
  ``TaskService``，单表写入委托给对应 CRUD）。
- 不负责：直接 SQL 操作（委托给 ``WorkspaceCrud``/``TaskCrud``）；单任务粒度的级联删除细节
  （委托给 ``TaskService``）。
"""

from contextlib import ExitStack
from pathlib import Path

from app.config.logging.logger import log
from app.models import TaskRecord, WorkspaceRecord
from app.models.errors.deletion_errors import DeletionBusyError
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.service.task_service import TaskDeletionResult
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations


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
            从 service 依赖入口取得 CRUD / service 单例并保存引用。
        """

        self._workspace = service_depends.get_workspace_crud()
        self._task_crud = service_depends.get_task_crud()
        self._task_service = service_depends.get_task_service()

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """创建工作区记录并在其根目录下初始化 ``.cosir`` 元数据区。

        参数:
            name: 工作区名称。
            root_path: 工作区根目录的绝对路径。

        返回:
            已持久化的 ``WorkspaceRecord``。

        异常:
            ValueError: 当 ``name`` 或 ``root_path`` 去除首尾空白后为空时抛出。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``workspaces`` 表插入一行记录；并在 ``<root_path>/.cosir`` 处创建元数据目录
            （已存在则幂等跳过）。元数据目录创建失败属于非致命降级：不阻断工作区创建，
            仅记 error 日志，便于事后排查。
        """
        normalized_path = self._normalize_root_path(root_path)
        if any(
            self._same_path(workspace.root_path, normalized_path)
            for workspace in self._workspace.list_all()
        ):
            raise ValueError("workspace root_path is already registered")
        record = self._workspace.create(
            name.strip() or Path(normalized_path).name,
            normalized_path,
        )

        cosir_dir = Path(normalized_path) / ".cosir"
        try:
            cosir_dir.mkdir(parents=True, exist_ok=True)
            log.info(
                "workspace_cosir_initialized",
                extra={
                    "msg": "workspace metadata dir initialized",
                    "data": {
                        "workspace_name": name,
                        "root_path": normalized_path,
                        "cosir_dir": str(cosir_dir),
                    },
                },
            )
        except OSError as exc:
            # 元数据目录非运行关键路径：降级处理，避免阻断工作区创建。
            log.error(
                "workspace_cosir_init_failed",
                extra={
                    "msg": "failed to initialize workspace metadata dir, skipped",
                    "data": {
                        "workspace_name": name,
                        "root_path": normalized_path,
                        "cosir_dir": str(cosir_dir),
                        "error": str(exc),
                        "errno": getattr(exc, "errno", None),
                    },
                },
            )

        return record

    @staticmethod
    def _normalize_root_path(root_path: str) -> str:
        """校验并规范化工作区根目录。"""
        candidate = Path(root_path.strip()).expanduser()
        if not candidate.is_absolute():
            raise ValueError("workspace root_path must be an absolute path")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError("workspace root_path must be an existing directory")
        return str(candidate.resolve())

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        """按当前操作系统规则比较两个规范化目录路径。"""
        return (
            Path(left).resolve().as_posix().casefold()
            == Path(right).resolve().as_posix().casefold()
        )

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

    def get_workspace(self, workspace_id: int) -> WorkspaceRecord:
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

    def create_task(self, workspace_id: int, title: str) -> TaskRecord:
        """在 workspace 结构性写闸门内创建用户 task 容器。

        参数:
            workspace_id: 所属 workspace 标识。
            title: task 标题。

        返回:
            新建的 task 记录。

        异常:
            KeyError: 如果 workspace 不存在。
            DeletionBusyError: 如果 workspace 正在结构性删除。
            sqlalchemy.exc.SQLAlchemyError: 如果 task 创建失败。

        副作用:
            向主库写入一个 task；不会启动 ConversationRun。
        """

        try:
            with workspace_operations.operation(workspace_id, timeout=10):
                self._workspace.get(workspace_id)
                return self._task_service.get_or_create_task(workspace_id, title)
        except TimeoutError as exc:
            raise DeletionBusyError("workspace", workspace_id) from exc

    def delete_workspace(self, workspace_id: int) -> None:
        """在单个事务内原子删除 workspace 及其全部 task 产物。

        删除前先校验工作区存在（不存在则抛 ``KeyError``），取得 workspace 闸门和所有现存 task
        的 task 闸门，再在单个 ``BEGIN IMMEDIATE`` 事务内重新读取 task 树。所有 task 产物与
        workspace 行必须一并提交；任一步失败都会整体回滚。提交后才回收 checkpoint 和卸载
        进程内 runtime space。

        参数:
            workspace_id: 待删除的工作区标识。

        返回:
            无。

        异常:
            KeyError: 如果指定工作区不存在。
            DeletionBusyError: 如果 workspace 或其 task 正在运行不可并发的操作。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败（事务回滚）。

        副作用:
            删除工作区下全部 task 树（task / turn / run / trace / delegation / file_snapshot /
            context / snapshot）及其孤儿 checkpoint；并删除 ``workspaces`` 表记录（旧 Runtime
            事件体系已删除，不再参与级联删除）。
        """

        self._workspace.get(workspace_id)
        try:
            with workspace_operations.operation(workspace_id, timeout=10):
                task_ids = set(self._task_crud.list_ids_by_workspace(workspace_id))
                with ExitStack() as stack:
                    self._acquire_task_operations(workspace_id, task_ids, stack)
                    log.info(
                        "workspace_delete_start",
                        extra={
                            "msg": "workspace delete started",
                            "data": {"workspace_id": workspace_id},
                        },
                    )
                    deleted_task_ids: set[int] = set()
                    orphan_threads: set[str] = set()
                    with begin_immediate(main_session_factory()) as session:
                        self._workspace.get_in_session(session, workspace_id)
                        root_task_ids = self._task_crud.list_root_ids_by_workspace(
                            workspace_id, session=session
                        )
                        for task_id in root_task_ids:
                            result = self._task_service.delete_task_tree_in_session(
                                task_id, session
                            )
                            deleted_task_ids.update(result.task_ids)
                            orphan_threads.update(result.orphan_checkpoint_threads)
                        remaining_task_ids = set(
                            self._task_crud.list_ids_by_workspace(
                                workspace_id, session=session
                            )
                        )
                        if remaining_task_ids:
                            raise RuntimeError(
                                "workspace contains task rows outside a deletable task tree: "
                                f"{sorted(remaining_task_ids)}"
                            )
                        self._workspace.delete_in_session(session, workspace_id)
                self._task_service.finalize_deleted_task_spaces(
                    TaskDeletionResult(
                        task_ids=frozenset(deleted_task_ids),
                        orphan_checkpoint_threads=frozenset(orphan_threads),
                    )
                )
        except TimeoutError as exc:
            raise DeletionBusyError("workspace", workspace_id) from exc
        log.info(
            "workspace_deleted",
            extra={"msg": "workspace deleted", "data": {"workspace_id": workspace_id}},
        )

    def _acquire_task_operations(
        self,
        workspace_id: int,
        task_ids: set[int],
        stack: ExitStack,
    ) -> None:
        """在 workspace 闸门内取得并刷新全部 task 操作闸门。

        参数:
            workspace_id: workspace 标识。
            task_ids: 初始读取到的 task 标识集合。
            stack: 负责在调用方离开时逆序释放锁的 ``ExitStack``。

        返回:
            无。锁由传入的 ``ExitStack`` 持有并在调用方退出时释放。

        异常:
            TimeoutError: 任一 task 在超时时间内无法取得操作闸门。

        副作用:
            按 task id 升序取得 task 闸门；在取得已有 task 闸门后再次读取 workspace，
            覆盖运行中的父 task 在等待期间创建的委派子 task。
        """

        locked_ids: set[int] = set()
        pending_ids = set(task_ids)
        while pending_ids:
            for task_id in sorted(pending_ids):
                stack.enter_context(
                    task_runtime_spaces.get_or_create(task_id).operation(timeout=10)
                )
                locked_ids.add(task_id)
            current_ids = set(self._task_crud.list_ids_by_workspace(workspace_id))
            pending_ids = current_ids - locked_ids
