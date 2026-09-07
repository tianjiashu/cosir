"""Task orchestration service.

单一职责：编排任务创建（仅建 task 容器，首轮次由调用方显式创建）与执行态派生。

职责边界：
- 负责：任务容器创建（不含首轮次）、从最新 turn 派生执行态、任务树原子级联删除
  （委托给 ``CascadeDeleter``）。
- 不负责：直接 SQL 操作（委托给 ``TaskCrud``/``ConversationRunCrud``/``WorkspaceCrud``/
  ``CascadeDeleter``）；不写执行态（执行态由 ``Turn`` 持有，本 service 仅派生展示）；
  不绑定 agent（agent 维度由 turn 与 delegation 记录承载）。
"""

import asyncio

from sqlalchemy.orm.session import Session

from app.config.logging.logger import log
from app.models import ConversationRunRecord, ConversationRunStatus, TaskRecord
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_TERMINAL_RUN_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.FAILED.value,
        ConversationRunStatus.CANCELLED.value,
    }
)


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
        self._cascade_deleter = service_depends.get_cascade_deleter()
        self._context = service_depends.get_conversation_task_context_service()
        self._snapshot = service_depends.get_conversation_task_snapshot_service()
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

    def update_context_usage(self, task_id: int, used: int) -> TaskRecord:
        """持久化任务最近一次上下文窗口已用 token。

        供运行时在每次模型步产出上下文占用事件后调用，使「打开历史任务」时可回显
        该任务最近一次的真实占用。total 不落库，由 ``resolve_context_window``
        动态计算（见 ``get_task`` API）。

        参数:
            task_id: 任务标识。
            used: 最近一次上下文窗口已用 token 数。

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

        return self._task.update_context_usage(task_id, used)

    def is_fork_available(self, task_id: int) -> bool:
        """返回任务的所有 Run 是否均处于已知终态。"""

        return not any(
            run.status not in _TERMINAL_RUN_STATUSES
            for run in self._turn.list_by_task(task_id)
        )

    async def fork_task(self, source_task_id: int, source_run_id: int) -> TaskRecord:
        """在指定历史 Run 处创建一个独立的 fork Task。"""

        supervisor = asyncio.create_task(
            self._fork_task_with_lock(source_task_id, source_run_id)
        )
        try:
            return await asyncio.shield(supervisor)
        except asyncio.CancelledError:
            # 持锁与 SQLite worker 属于 supervisor，而不是 HTTP 请求协程；请求
            # 被重复取消时仍由 supervisor 完成事务并负责释放 run_lock。
            supervisor.add_done_callback(self._consume_detached_fork_result)
            raise

    async def _fork_task_with_lock(
        self, source_task_id: int, source_run_id: int
    ) -> TaskRecord:
        """在独立 supervisor 中持有源 Task 执行锁并运行同步 Fork 事务。"""

        source_space = task_runtime_spaces.get_or_create(source_task_id)
        # 与 ConversationRunExecutor 共用 run_lock，等待执行/取消完全收束后再
        # 把 DB、context 和 snapshot 作为同一个历史边界复制。
        async with source_space.run_lock:
            return await asyncio.to_thread(
                self._fork_task_locked, source_task_id, source_run_id
            )

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
        新主键通过 ``ConversationRunCrud.clone_for_task`` 生成，再用映射表改写 context
        与 snapshot；不触发普通 run 创建事件，也不复制 LangGraph checkpoint 内容。

        参数:
            source_task_id: 被 fork 的源 Task 标识。
            source_run_id: 历史边界 Run 标识，复制范围包含该 Run。

        返回:
            已提交的目标 fork Task。

        异常:
            KeyError: 源 Task 或边界 Run 不存在/不属于源 Task。
            TaskForkConflictError: 源 Task 存在活动 Run。
            SnapshotNotReadyError: 源 snapshot 缺失或无法通过校验。

        副作用:
            新增目标 Task、历史 cloned Runs、context entries 与 idle snapshot；源 Task 不变。
        """

        source_space = task_runtime_spaces.get_or_create(source_task_id)
        with source_space.lock:
            cloned_snapshot = None
            with self._session_factory.begin() as session:
                source = self._task.ensure_task(session, source_task_id)
                runs = self._turn.list_by_task_in_session(session, source_task_id)
                if any(run.status not in _TERMINAL_RUN_STATUSES for run in runs):
                    raise TaskForkConflictError(
                        "TASK_NOT_READY",
                        "all runs must be terminal before forking",
                    )

                boundary_index = next(
                    (
                        index
                        for index, run in enumerate(runs)
                        if run.id == source_run_id
                    ),
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
                    title=self._task.next_fork_title(
                        source.workspace_id, source.title, session
                    ),
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
                    cloned = self._turn.clone_for_task(
                        session, source_run, target.id
                    )
                    run_id_map[source_run.id] = cloned.id

                self._context.clone_for_fork(
                    source_task_id,
                    target.id,
                    run_id_map,
                    session,
                )
                cloned_snapshot = self._snapshot.clone_for_fork(
                    source_task_id,
                    target.id,
                    run_id_map,
                    session,
                )

            if cloned_snapshot is None:
                raise RuntimeError("fork snapshot was not produced")
            source_manager = source_space.existing_context_manager()
            if source_manager is not None:
                try:
                    task_runtime_spaces.get_or_create(
                        target.id
                    ).install_fork_context_manager(
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
            try:
                self._snapshot.cache_committed_snapshot(target.id, cloned_snapshot)
            except Exception:
                log.exception(
                    "task_fork_snapshot_cache_failed",
                    extra={
                        "msg": "fork 目标 snapshot 缓存失败，持久化数据仍可重新读取",
                        "data": {"target_task_id": target.id},
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

        删除前先校验任务存在（不存在则抛 ``KeyError``），再通过
        ``CascadeDeleter.delete_task_tree`` 在单个 ``BEGIN IMMEDIATE`` 写锁事务内
        递归清理其下全部子任务（委派子任务）及各自轮次、消息轨迹、运行时事件、文件
        快照与委派记录，保证原子性（全删或全不删）与并发安全（删除期间无并发写插入
        孤儿数据）。删除是高风险操作，保留 start / complete 审计日志。

        参数:
            task_id: 待删除的任务标识。

        返回:
            无。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``turn_messages`` / ``file_snapshots`` / ``conversation_runs`` / ``delegations``
            / ``tasks`` 表删除该任务树相关数据（旧 Runtime 事件体系已随对话事实重构
            一并删除，不再参与级联删除）。
        """

        self._task.get(task_id)  # 存在性守卫，不存在抛 KeyError
        log.info(
            "task_delete_start",
            extra={"msg": "task delete started", "data": {"task_id": task_id}},
        )
        deleted_count = self._cascade_deleter.delete_task_tree(task_id)
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {"task_id": task_id, "deleted_tasks": deleted_count},
            },
        )
