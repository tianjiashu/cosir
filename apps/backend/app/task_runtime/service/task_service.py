"""Task orchestration service.

单一职责：编排任务创建（仅建 task 容器，首轮次由调用方显式创建）与执行态派生。

职责边界：
- 负责：任务容器创建（不含首轮次）、从最新 turn 派生执行态、任务树原子级联删除
  （编排 ``TaskCrud``/``ConversationRunCrud``/``ConversationCommandCrud``/
  ``ConversationTaskContextCrud``/
  ``DelegationCrud`` 在单事务内逐个清理，孤儿 checkpoint 线程交 ``checkpoint_gc`` 回收）。
- 不负责：直接 SQL 操作（委托给上述 CRUD）；不写执行态（执行态由 ``Turn`` 持有，本
  service 仅派生展示）；不绑定 agent（agent 维度由 turn 与 delegation 记录承载）。
"""

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass

from sqlalchemy.orm.session import Session

from app.config.logging.logger import log
from app.models import ConversationRunRecord, ConversationRunStatus, TaskRecord
from app.models.errors.deletion_errors import DeletionBusyError, RunDeletionConflictError
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.service import depends as service_depends
from app.storage.checkpoint_gc import cleanup_orphan_checkpoint_threads
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations

_TERMINAL_RUN_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.FAILED.value,
        ConversationRunStatus.CANCELLED.value,
    }
)


@dataclass(frozen=True)
class TaskDeletionResult:
    """一次已提交任务级联删除需要执行的进程内清理信息。"""

    task_ids: frozenset[int]
    orphan_checkpoint_threads: frozenset[str]


class TaskService:
    """Orchestrate task creation, lifecycle management, and execution-status derivation."""

    def __init__(self) -> None:
        """初始化任务 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._task_register = task_runtime_spaces
        self._task = service_depends.get_task_crud()
        self._turn = service_depends.get_conversation_run_crud()
        self._workspace = service_depends.get_workspace_crud()
        self._context = service_depends.get_conversation_task_context_service()
        self._state = service_depends.get_conversation_task_state_service()
        self._command = service_depends.get_conversation_command_crud()
        self._delegation = service_depends.get_delegation_crud()
        self._task_context_crud = service_depends.get_conversation_task_context_crud()
        self._session_factory = main_session_factory()

    def get_or_create_task(
        self,
        workspace_id: int,
        title: str,
        task_id: int | None = None,
        creation_command_id: str | None = None,
        task_type: str = "user",
        parent_task_id: int | None = None,
        parent_run_id: int | None = None,
        delegation_id: int | None = None,
        session: Session | None = None,
        extra: dict[str, object] | None = None,
    ) -> TaskRecord:
        if workspace_id is None:
            raise ValueError("workspace_id is None")

        if task_id is None:
            return self._task.create(
                workspace_id=workspace_id,
                title=title,
                task_type=task_type,
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                delegation_id=delegation_id,
                creation_command_id=creation_command_id,
                extra=extra,
                session=session,
            )
        task_runtime_spaces.get_or_create(task_id)
        return self._task.get(task_id)

    def get_latest_run(self, task_id: int) -> ConversationRunRecord | None:
        """返回某任务下创建时间最新的 run。

        委托 ``ConversationRunCrud.get_latest_by_task`` 完成单条倒序查询；任务不含任何
        run 时返回 ``None``。该方法不做任务存在性守卫——调用方若在 run 之前先取过 task
        则天然存在，仅取最新 run 的场景下 ``None`` 已能表达「该 task 尚无 run」。

        参数:
            task_id: 任务标识。

        返回:
            该任务下最新（``created_at`` 再 ``id`` 倒序）的 ``ConversationRunRecord``；
            任务无任何 run 时返回 ``None``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """

        return self._turn.get_latest_by_task(task_id)

    def get_context_window_total(self, task_id: int) -> int | None:
        """返回 Task 持久化的上下文窗口上限。

        参数:
            task_id: 任务标识。

        返回:
            ``tasks.context_window_total`` 的持久化值；尚未记录时为 ``None``。

        异常:
            底层任务读取异常会原样抛出。

        副作用:
            无（仅读取 Task 持久化事实）。
        """

        return self._task.get(task_id).context_window_total

    def get_task(self, task_id: int) -> TaskRecord:
        """按标识取单个任务，并附带派生的执行态。

        参数:
            task_id: 任务标识。

        返回:
            对应的 ``TaskRecord``，其 ``execution_status`` 由最新轮次派生
            （见 ``task_display_status``）。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task.get(task_id)

    def update_context_usage(
        self, task_id: int, used: int, context_window_total: int | None = None
    ) -> TaskRecord:
        """持久化任务最近一次上下文占用与对应窗口上限。

        供运行时在每次模型步产出上下文占用事件后调用，使「打开历史任务」时可回显
        该任务最近一次的真实占用与窗口上限。

        参数:
            task_id: 任务标识。
            used: 最近一次上下文窗口已用 token 数。
            context_window_total: 本次 Run 使用的模型上下文窗口上限。

        返回:
            更新后的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            更新 ``tasks`` 表对应行的 context_usage_used 与 updated_at。
        """

        if used < 0:
            # 已用 token 不可能为负；调用方传入负数属异常数据，clamp 为 0 并告警，
            # 避免脏数据落库影响前端占比展示。
            log.warning(
                "context_usage_negative_clamped",
                extra={
                    "msg": "已用 token 为负，按 0 处理",
                    "data": {"task_id": task_id, "used": used},
                },
            )
            used = 0

        return self._task.update_context_usage(task_id, used, context_window_total)

    def is_fork_available(self, task_id: int) -> bool:
        """返回任务的所有 Run 是否均处于已知终态。"""

        return not any(
            run.status not in _TERMINAL_RUN_STATUSES for run in self._turn.list_by_task(task_id)
        )

    async def fork_task(self, source_task_id: int, source_run_id: int) -> TaskRecord:
        """在指定历史 Run 处创建一个独立的 fork Task。"""

        supervisor = asyncio.create_task(self._fork_task_with_lock(source_task_id, source_run_id))
        try:
            return await asyncio.shield(supervisor)
        except asyncio.CancelledError:
            # 持锁与 SQLite worker 属于 supervisor，而不是 HTTP 请求协程；请求
            # 被重复取消时仍由 supervisor 完成事务并负责释放 Task 操作闸门。
            supervisor.add_done_callback(self._consume_detached_fork_result)
            raise

    async def _fork_task_with_lock(self, source_task_id: int, source_run_id: int) -> TaskRecord:
        """在独立 supervisor 中运行带 workspace/task 锁的同步 Fork 事务。"""

        return await asyncio.to_thread(self._fork_task_locked, source_task_id, source_run_id)

    @staticmethod
    def _consume_detached_fork_result(task: asyncio.Task[TaskRecord]) -> None:
        """消费被取消请求遗留的 supervisor 结果，避免后台异常无人观察。"""

        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception(
                "task_fork_detached_worker_failed",
                extra={"msg": "已取消请求的 fork supervisor 执行失败"},
            )

    def _fork_task_locked(self, source_task_id: int, source_run_id: int) -> TaskRecord:
        """在指定历史 Run 处创建一个独立的 fork Task。

        所有跨表写入使用同一个 SQLite 事务，并在源 Task runtime lock 内完成。Run 的
        新主键通过 ``ConversationRunCrud.clone_for_task`` 生成，再用映射表改写 context；
        不触发普通 run 创建事件，也不复制 LangGraph checkpoint 内容。

        参数:
            source_task_id: 被 fork 的源 Task 标识。
            source_run_id: 历史边界 Run 标识，复制范围包含该 Run。

        返回:
            已提交的目标 fork Task。

        异常:
            KeyError: 源 Task 或边界 Run 不存在/不属于源 Task。
            TaskForkConflictError: 源 Task 存在活动 Run。
        副作用:
            新增目标 Task、历史 cloned Runs 与 context entries；源 Task 不变。
        """

        source = self._task.get(source_task_id)
        try:
            with workspace_operations.operation(source.workspace_id, timeout=10):
                source_space = task_runtime_spaces.get_or_create(source_task_id)
                with source_space.operation(timeout=10):
                    return self._fork_task_locked_in_operation(source_task_id, source_run_id)
        except TimeoutError as exc:
            raise DeletionBusyError("task", source_task_id) from exc

    def _fork_task_locked_in_operation(
        self,
        source_task_id: int,
        source_run_id: int,
    ) -> TaskRecord:
        """在已持有 workspace/task 闸门时执行 Fork 数据库事务。"""

        with begin_immediate(self._session_factory) as session:
            source = self._task.ensure_task(session, source_task_id)
            runs = self._turn.list_by_task_in_session(session, source_task_id)
            if any(run.status not in _TERMINAL_RUN_STATUSES for run in runs):
                raise TaskForkConflictError(
                    "TASK_NOT_READY",
                    "all runs must be terminal before forking",
                )

            boundary_index = next(
                (index for index, run in enumerate(runs) if run.id == source_run_id),
                None,
            )
            if boundary_index is None:
                raise KeyError(source_run_id)
            source_prefix = runs[: boundary_index + 1]
            if any(run.status not in _TERMINAL_RUN_STATUSES for run in source_prefix):
                raise TaskForkConflictError(
                    "RUN_NOT_READY",
                    "the selected run must be terminal before forking",
                )

            target = self._task.create(
                workspace_id=source.workspace_id,
                title=self._task.next_fork_title(source.workspace_id, source.title, session),
                task_type="fork",
                extra={
                    "fork": {
                        "source_task_id": source_task_id,
                        "source_run_id": source_run_id,
                    }
                },
                session=session,
            )

            run_id_map: dict[int, int] = {}
            for source_run in source_prefix:
                cloned = self._turn.clone_for_task(session, source_run, target.id)
                run_id_map[source_run.id] = cloned.id

            cloned_context_count = self._context.clone_for_fork(
                source_task_id,
                target.id,
                run_id_map,
                session,
            )
        for _ in range(cloned_context_count):
            log.info(
                "context_message_persisted",
                extra={
                    "msg": "fork context message 已提交",
                    "data": {"task_id": target.id, "message_type": "cloned"},
                },
            )
        source_space = task_runtime_spaces.get_or_create(source_task_id)
        source_manager = source_space.existing_context_manager()
        if source_manager is not None:
            try:
                task_runtime_spaces.get_or_create(target.id).install_fork_context_manager(
                    source_manager.fork_context_manager(target.id)
                )
            except Exception:
                # DB 已提交后，持久化 context 仍是 canonical source；目标 space
                # 下次访问时会按 fork Task 懒加载，不能把可恢复的内存优化失败
                # 伪装成整个 fork 失败。
                log.exception(
                    "task_fork_runtime_hydration_failed",
                    extra={
                        "msg": "fork 目标运行时 hydrate 失败，将在后续访问时懒加载",
                        "data": {
                            "source_task_id": source_task_id,
                            "source_run_id": source_run_id,
                            "target_task_id": target.id,
                        },
                    },
                )
        log.info(
            "task_forked",
            extra={
                "msg": "task fork committed",
                "data": {
                    "source_task_id": source_task_id,
                    "source_run_id": source_run_id,
                    "target_task_id": target.id,
                },
            },
        )
        # target 已经在事务内 flush，且提交后的只读查询不是 Fork 成功条件；
        # 直接返回事务内记录，避免“已提交但最后 get 失败”伪装成 Fork 失败。
        return target

    def list_tasks_for_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """列出某工作区下的用户任务（排除委派子任务）。

        委派子任务（``task_type='delegation'``）不进侧边栏对话列表，故本方法仅返回
        ``task_type='user'`` 的任务，按更新时间倒序。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区下的用户任务记录列表（不含委派子任务）；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task.list_by_workspace(workspace_id)

    def delete_task(self, task_id: int) -> None:
        """原子删除任务树并级联清理其下全部子产物。

        删除前先校验任务存在（不存在则抛 ``KeyError``），再按固定的 workspace → task
        锁顺序取得结构性写操作闸门。在单个 ``BEGIN IMMEDIATE`` 事务内按「子任务先于父任务」
        的后序清理每个任务自身及其产物，保证原子性（全删或全不删）。
        提交后把孤儿 LangGraph checkpoint 线程交 ``checkpoint_gc.cleanup_orphan_checkpoint_threads``
        回收。删除是高风险操作，保留 start / complete 审计日志。

        参数:
            task_id: 待删除的任务标识。

        返回:
            无。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败（事务回滚）。

        副作用:
            从 ``conversation_task_contexts`` / ``conversation_commands`` / ``conversation_runs`` /
            ``delegations`` / ``tasks`` 表删除该任务树相关数据；并提交后清理已删任务遗留的
            孤儿 LangGraph checkpoint 线程、卸载进程内 runtime space。
        """

        task = self._task.get(task_id)
        workspace = self._workspace.get(task.workspace_id)
        try:
            with workspace_operations.operation(task.workspace_id, timeout=10):
                task = self._task.get(task_id)
                locked_ids = self._collect_task_ancestor_ids(task_id) | {task_id}
                with ExitStack() as stack:
                    for current_id in sorted(locked_ids):
                        stack.enter_context(
                            self._task_register.get_or_create(current_id).operation(timeout=10)
                        )
                    task_ids = self._collect_task_tree_ids(task_id)
                    for current_id in sorted(task_ids - locked_ids):
                        stack.enter_context(
                            self._task_register.get_or_create(current_id).operation(timeout=10)
                        )
                        locked_ids.add(current_id)
                    log.info(
                        "task_delete_start",
                        extra={"msg": "task delete started", "data": {"task_id": task_id}},
                    )
                    with begin_immediate(self._session_factory) as session:
                        result = self.delete_task_tree_in_session(task_id, session)
                    self.finalize_deleted_task_spaces(result)
                    self.collect_workspace_attachment_orphans(
                        workspace.id,
                        workspace.root_path,
                    )
        except TimeoutError as exc:
            raise DeletionBusyError("task", task_id) from exc
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {"task_id": task_id, "deleted_tasks": len(result.task_ids)},
            },
        )

    def delete_run(self, task_id: int, run_id: int) -> None:
        """删除单个 run 及其会话上下文与产物（checkpoint、终端元数据）。

        允许删除 task 内任意位置的 run（含中间），不重排剩余 context 的 ``sequence``。
        先校验 run 属于该 task（否则 ``KeyError``）；再按 workspace → task 锁顺序取得结构性
        写操作闸门。task 内存在 ``pending`` / ``running`` 的 active run 时拒绝删除（抛
        ``RunDeletionConflictError``），该判断在持有 task 闸门后执行，与 run 创建路径互斥。
        在单个 ``BEGIN IMMEDIATE`` 事务内按外键依赖逆序清理：context → command → delegation →
            ``tasks.parent_run_id`` 引用 → run 行（``tasks.current_run_id`` 由
        ``ON DELETE SET NULL`` 自动处理）。提交后回收本 run 遗留的孤儿 LangGraph checkpoint 线程。

        不新增 run 级进程内 snapshot 失效入口：Transport 快照刷新由前端在删除后重新拉取/重连
        处理（见架构边界：允许 context 与 snapshot 最终一致）。

        参数:
            task_id: run 所属任务标识。
            run_id: 待删除的 Conversation Run 标识。

        返回:
            无。

        异常:
            KeyError: 如果 task 或 run 不存在，或 run 不属于该 task。
            RunDeletionConflictError: 如果 task 内存在 active run。
            DeletionBusyError: 如果取得 workspace/task 闸门超时。
            sqlalchemy.exc.SQLAlchemyError: 如果删除事务失败（回滚）。

        副作用:
            从 ``conversation_task_contexts`` / ``conversation_commands`` / ``delegations`` /
            ``conversation_runs`` 删除该 run 相关行，并把 ``tasks`` 中
            ``parent_run_id`` 指向本 run 的引用置空；提交后回收孤儿 checkpoint 线程。
        """

        task = self._task.get(task_id)
        run = self._turn.get(run_id)
        if run.task_id != task_id:
            raise KeyError(run_id)
        checkpoint_thread = run.checkpoint_thread_id
        try:
            with workspace_operations.operation(task.workspace_id, timeout=10):
                space = self._task_register.get_or_create(task_id)
                with space.operation(timeout=10):
                    # active run 守卫必须在持有 task 闸门后执行：run 创建同样需要该闸门，
                    # 持锁后重查可避免 get 与持锁之间新启一个 active run 的竞态。
                    if self._turn.has_active_for_task(task_id):
                        raise RunDeletionConflictError(
                            "TASK_HAS_ACTIVE_RUN",
                            f"task {task_id} has an active run; run deletion is rejected",
                        )
                    service_depends.get_terminal_session_service().close_run_terminals(
                        run_id,
                        reason="run_deleted",
                    )
                    log.info(
                        "run_delete_start",
                        extra={
                            "msg": "run delete started",
                            "data": {"task_id": task_id, "run_id": run_id},
                        },
                    )
                    with begin_immediate(self._session_factory) as session:
                        self._context.delete_by_run_id(task_id, run_id, session=session)
                        self._command.delete_by_run_id(run_id, session=session)
                        self._delegation.delete_by_run_id(run_id, session=session)
                        self._task.clear_parent_run_id_by_run_id(run_id, session=session)
                        self._turn.delete_by_ids([run_id], session)
                        remaining_threads = (
                            self._turn.collect_checkpoint_threads_by_thread_ids(
                                session, {checkpoint_thread}
                            )
                        )
                    orphan_threads = {checkpoint_thread} - remaining_threads
                    if orphan_threads:
                        cleanup_orphan_checkpoint_threads(orphan_threads)
        except TimeoutError as exc:
            raise DeletionBusyError("task", task_id) from exc
        log.info(
            "run_deleted",
            extra={"msg": "run deleted", "data": {"task_id": task_id, "run_id": run_id}},
        )

    def collect_workspace_attachment_orphans(self, workspace_id: int, root_path: str) -> None:
        """在删除任务或工作区后清理未被剩余 Run 引用的 workspace 附件。

        这是提交后的旁路清理；任何文件系统异常都会记录并放弃本次清理，不影响已经提交
        的数据库删除结果。调用方应在 workspace 闸门内调用，以避免与任务删除并发。
        """

        try:
            from app.service.attachment.attachment_service import collect_workspace_orphans

            references: list[str] = []
            for remaining_task_id in self._task.list_ids_by_workspace(workspace_id):
                for run in self._turn.list_by_task(remaining_task_id):
                    references.extend(run.image_paths or [])
            collect_workspace_orphans(root_path, references)
        except Exception:
            log.exception(
                "attachment_orphan_gc_failed",
                extra={
                    "msg": "图片附件孤儿清理失败，保留文件供后续恢复",
                    "data": {"workspace_id": workspace_id},
                },
            )

    def _collect_task_ancestor_ids(self, task_id: int) -> set[int]:
        """收集 task 自身以上的父 task 标识。

        参数:
            task_id: 起始 task 标识。

        返回:
            从起始 task 向上的全部父 task 标识，不含起始 task。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果读取父子关系失败。

        副作用:
            无；仅读取 task 关系。关系异常形成环时停止遍历，避免删除请求死循环。
        """

        ancestors: set[int] = set()
        current = self._task.get(task_id)
        while current.parent_task_id is not None:
            parent_id = current.parent_task_id
            if parent_id in ancestors or parent_id == task_id:
                break
            ancestors.add(parent_id)
            current = self._task.get(parent_id)
        return ancestors

    def _collect_task_tree_ids(self, root_task_id: int, session: Session | None = None) -> set[int]:
        """收集待删除任务树的全部 task id。

        参数:
            root_task_id: 任务树根节点。

        返回:
            包含根任务和递归委派子任务的 task id 集合。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询任务树失败。

        副作用:
            无；只读取任务关系。
        """

        collected: set[int] = set()
        frontier = [root_task_id]
        while frontier:
            current_id = frontier.pop()
            if current_id in collected:
                continue
            collected.add(current_id)
            frontier.extend(
                child.id for child in self._task.list_by_parent_task(current_id, session=session)
            )
        return collected

    def _collect_task_tree_ids_postorder(
        self, root_task_id: int, session: Session | None = None
    ) -> list[int]:
        """收集任务树全部 id 并按「子任务先于父任务」的后序返回。

        单条 ``DELETE ... WHERE id IN (...)`` 无法保证父行晚于子行，而 ``tasks.parent_task_id``
        自引用外键要求父任务行在其所有子任务行之后删除，否则 SQLite 即时外键检查会报
        ``FOREIGN KEY constraint failed``。故先收集整棵任务树，再以「被本批任务引用为父的
        节点暂留、其余为叶子」的拓扑方式逐层产出后序列表（叶子在前、根在后）。

        参数:
            root_task_id: 任务树根节点。

        返回:
            后序排列的 task id 列表（子任务在前、根任务在后）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询任务关系失败。

        副作用:
            无；只读取任务关系。
        """

        all_ids = self._collect_task_tree_ids(root_task_id, session=session)
        parent_of: dict[int, int] = {}
        for current_id in all_ids:
            for child in self._task.list_by_parent_task(current_id, session=session):
                parent_of[child.id] = current_id

        remaining = set(all_ids)
        order: list[int] = []
        while remaining:
            parents_in_remaining = {parent_of[tid] for tid in remaining if tid in parent_of}
            leaves = [tid for tid in remaining if tid not in parents_in_remaining]
            if not leaves:
                # 防御性兜底：仅当任务关系出现环时触发，直接收尾避免死循环。
                order.extend(remaining)
                break
            order.extend(leaves)
            remaining -= set(leaves)
        return order

    def delete_task_tree_in_session(
        self, root_task_id: int, session: Session
    ) -> TaskDeletionResult:
        """在调用方事务内删除一棵 task 树，不提交也不做进程内清理。

        参数:
            root_task_id: 待删除任务树的根 task 标识。
            session: 已开启的主库事务 session；调用方必须先取得相关 runtime 闸门。

        返回:
            包含已删除 task 标识和待提交后回收的 checkpoint thread 标识的结果。

        异常:
            KeyError: 如果根 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            在当前事务中删除 task 树及全部子产物；不提交、不卸载 runtime space。
        """

        task_ids_postorder = self._collect_task_tree_ids_postorder(root_task_id, session=session)
        orphan_threads: set[str] = set()
        for current_id in task_ids_postorder:
            orphan_threads |= self._delete_single_task_in_session(current_id, session)
        return TaskDeletionResult(
            task_ids=frozenset(task_ids_postorder),
            orphan_checkpoint_threads=frozenset(orphan_threads),
        )

    def finalize_deleted_task_spaces(self, result: TaskDeletionResult) -> None:
        """执行已提交 task 删除的进程内 runtime 清理和 checkpoint GC。

        参数:
            result: 已提交的 task 删除结果。

        返回:
            无。

        异常:
            无。checkpoint GC 本身失败安全；runtime 清理异常由调用方观察并记录。

        副作用:
            标记并卸载已删除 task 的 runtime 投影，回收不再被主库引用的 checkpoint。
        """

        cleanup_orphan_checkpoint_threads(set(result.orphan_checkpoint_threads))
        self._unload_deleted_task_spaces(set(result.task_ids))

    def _unload_deleted_task_spaces(self, task_ids: set[int]) -> None:
        """清除已删除任务的进程内 runtime space 与 context 引用。"""

        for current_id in task_ids:
            try:
                self._task_register.mark_deleted(current_id)
                self._state.mark_task_deleted(current_id)
                projector = service_depends.get_conversation_event_projector()
                mark_projector_deleted = getattr(projector, "mark_task_deleted", None)
                if callable(mark_projector_deleted):
                    mark_projector_deleted(current_id)
                executor = service_depends.get_conversation_run_executor()
                mark_executor_deleted = getattr(executor, "mark_task_deleted", None)
                if callable(mark_executor_deleted):
                    mark_executor_deleted(current_id)
                try:
                    from app.config.configuration import get_tool_system

                    clear_tool_state = getattr(get_tool_system().executor, "clear_task_state", None)
                    if callable(clear_tool_state):
                        clear_tool_state(current_id)
                except RuntimeError:
                    # 删除可以发生在工具系统尚未完成装配的测试/启动边界；此时没有
                    # 进程内工具状态需要清理，数据库删除仍然是 canonical 结果。
                    pass
                space = self._task_register.get(current_id)
                if space is None:
                    continue
                space.unload_context_manager()
                self._task_register.unload(current_id, expected_space=space)
            except Exception:
                log.exception(
                    "task_runtime_cleanup_failed",
                    extra={
                        "msg": "task runtime cleanup failed after committed deletion",
                        "data": {"task_id": current_id},
                    },
                )

    def delete_single_task(self, task_id: int, session: Session | None = None) -> set[str]:
        """删除单个任务及其全部产物，不递归删除其子任务。

        给定单个 task_id，在单一事务内清理该任务自身及其全部产物（run / command / context /
        snapshot / delegation），但不触碰子任务行。任务树的收集与级联删除由上层
        编排（见 ``delete_task``）：上层负责以「子任务先于父任务」的后序顺序逐个调用本方法，
        本方法只负责单任务粒度的删除，并在独立事务（无外部 session 时）提交后清理该任务
        遗留的孤儿 LangGraph checkpoint 线程。

        外键前置条件（由上层保证）：因 ``tasks`` 与 ``conversation_runs`` 存在双向外键环
        （``tasks.parent_run_id → runs``、``tasks.delegation_id → delegations``），且
        ``tasks.parent_task_id`` 自引用指向父任务，本方法在删除 run / delegation / task 行前
        会先解除本任务行对 run 与 delegation 的引用；而子任务必须在父任务之前删除（后序），
        以避免自引用外键冲突。

        参数:
            task_id: 待删除任务的标识（整数 id）。
            session: 可选外部事务 session。传入时复用该事务（调用方负责提交、子任务后序
                编排、space 卸载与 checkpoint GC）；为 None 时由本方法自开事务并自动提交，
                随后清理孤儿 checkpoint 线程。

        返回:
            本次删除不再被主库剩余 run 引用的 checkpoint thread id 集合；传入外部 session 时
            同样返回该集合，由调用方在提交后统一做 checkpoint GC。

        异常:
            KeyError: 如果 task_id 对应的任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败（事务回滚）。

        副作用:
            在事务内删除该任务的产物与任务行；无外部 session 时额外清理孤儿 checkpoint 线程；
            不触碰子任务、不卸载 runtime space（均由上层负责）。
        """

        if session is None:
            with begin_immediate(self._session_factory) as session:
                orphan_threads = self._delete_single_task_in_session(task_id, session)
            if orphan_threads:
                cleanup_orphan_checkpoint_threads(orphan_threads)
            return orphan_threads
        return self._delete_single_task_in_session(task_id, session)

    def _delete_single_task_in_session(self, task_id: int, session: Session) -> set[str]:
        """在调用方事务内删除单个任务及其产物，返回孤儿 checkpoint 线程集合。

        顺序：先解除本任务行对 run / delegation 的引用（双向外键环），再按外键依赖逆序
        删除 context / delegation / command / run，最后删除 task 行。
        delegation 同时按 ``task_id`` 与 ``child_task_id`` 删除，覆盖本任务发起的委派与
        创建本任务的委派记录。

        参数:
            task_id: 待删除任务的标识。
            session: 处于事务中的 SQLAlchemy session（本方法不提交）。

        返回:
            已不再被主库剩余 run 引用的 checkpoint thread id 集合。

        异常:
            KeyError: 如果任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果任一删除失败（由调用方回滚）。

        副作用:
            在 session 内删除该任务的产物与任务行；不提交、不卸载 space。
        """

        self._task.ensure_task(session, task_id)  # 存在性守卫
        # 解除 tasks.parent_run_id / delegation_id 对 run、delegation 的引用（双向外键环）。
        self._task.clear_parent_run_id([task_id], session)
        self._task.clear_delegation_id([task_id], session)

        checkpoint_threads = self._turn.collect_checkpoint_threads_by_task_ids(session, [task_id])
        run_ids = self._turn.collect_run_ids_by_task_ids(session, [task_id])

        self._task_context_crud.delete_by_task_ids([task_id], session)
        # delegation 同时覆盖 task_id / child_task_id 两个外键方向。
        self._delegation.delete_by_task_ids([task_id], session)
        # command.run_id 外键指向 run，必须先删 command 再删 run。
        self._command.delete_by_task_ids([task_id], session)
        service_depends.get_terminal_session_service().delete_task_sessions([task_id])
        self._turn.delete_by_ids(run_ids, session)
        self._task.delete_by_ids([task_id], session)

        remaining_threads = self._turn.collect_checkpoint_threads_by_thread_ids(
            session, checkpoint_threads
        )
        return checkpoint_threads - remaining_threads
