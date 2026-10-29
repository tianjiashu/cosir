"""Task orchestration service.

单一职责：编排任务创建（仅建 task 容器，首轮次由调用方显式创建）与执行态派生。

职责边界：
- 负责：任务容器创建（不含首轮次）、从最新 turn 派生执行态、任务树原子级联删除
  （编排 ``TaskCrud``/``ConversationRunCrud``/``ConversationTaskContextCrud`` 在单事务内逐个清理，
  Run 执行进度随记录级联删除）。
- 不负责：直接 SQL 操作（委托给上述 CRUD）；不写执行态（执行态由 ``Turn`` 持有，本
  service 仅派生展示）；不绑定 agent（agent 维度由 turn 与 delegation 记录承载）。
"""

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from typing import cast

from sqlalchemy.orm.session import Session

from app.config.logging.logger import log
from app.models import ConversationRunRecord, TaskRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES
from app.models.errors.deletion_errors import (
    DeletionBusyError,
    ProvisionalTaskCleanupConflictError,
)
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.models.message_metadata import UserMessageMetadata
from app.service import depends as service_depends
from app.storage.queries.task_read_queries import TaskReadQueries
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations


@dataclass(frozen=True)
class TaskDeletionResult:
    """一次已提交任务级联删除需要执行的进程内清理信息。"""

    task_ids: frozenset[int]


@dataclass(frozen=True)
class _ForkPoint:
    """fork 切分点：以某条用户消息开头的一轮，或某个 Run 开头、没有用户输入的回复。"""

    user_message_id: int | None
    leading_run_id: int | None
    source_message_id: str


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
        self._task_context_crud = service_depends.get_conversation_task_context_crud()
        self._session_factory = main_session_factory()
        self._task_reads = TaskReadQueries(self._session_factory)

    def get_or_create_task(
        self,
        workspace_id: int,
        title: str,
        task_id: int | None = None,
        creation_command_id: str | None = None,
        task_type: str = "user",
        parent_task_id: int | None = None,
        session: Session | None = None,
        extra: dict[str, object] | None = None,
        tool_schemas: list[dict[str, object]] | None = None,
    ) -> TaskRecord:
        """创建新 Task 或读取已有 Task，并在新建时固化工具 schema。

        新建用户 Task 时，``tool_schemas`` 由调用方显式提供并固化，本方法不会自行从
        主 Agent profile 或工具注册表推导（设计意图上曾希望以当前 profile 与工具注册表
        解析出的 schema 作为默认来源，但当前实现未内置该推导，统一交由调用方负责——此为
        有意为之，以保持本方法的通用性）。传入 ``None`` 时 ``tasks.tool_schemas`` 会
        持久化为**空列表**，意味着该 Task 运行期无任何可调用工具（运行期
        ``task_tool_schemas`` 为空 → ``allows_tools`` 为空）。主 Agent Task 的 schema 由
        ``WorkspaceService._main_agent_tool_schemas`` 解析后传入，委派/Team 节点等由各自
        调用方筛选后传入。已有 Task 只读取持久化记录，不重新根据当前 profile 改写工具集合。

        参数:
            workspace_id: 所属工作区标识。
            title: Task 标题。
            task_id: 已有 Task 标识；非空时只执行读取。
            creation_command_id: provisional Task 的创建命令标识。
            task_type: Task 类型。
            parent_task_id: 委派子 Task 的父 Task 标识。
            session: 可选的外部数据库事务会话。
            extra: Task 扩展 JSON。
            tool_schemas: 新建 Task 时固化的固定模型工具 schema；传入 ``None`` 会持久化
                为空列表（即该 Task 无可用工具），调用方须显式提供已筛选的 schema。

        返回:
            新建或读取的 ``TaskRecord``。

        异常:
            RuntimeError: 主 Agent / 工具注册表尚未完成装配，或已固化 schema 无法生成。
            KeyError: 读取不存在的已有 Task。

        副作用:
            新建路径向 ``tasks.tool_schemas`` 写入 schema；已有 Task 路径只初始化运行时
            space 并读取，不修改数据库。
        """

        if workspace_id is None:
            raise ValueError("workspace_id is None")

        if task_id is None:
            frozen_schemas = [] if tool_schemas is None else tool_schemas
            task = self._task.create(
                workspace_id=workspace_id,
                title=title,
                task_type=task_type,
                parent_task_id=parent_task_id,
                creation_command_id=creation_command_id,
                extra=extra,
                tool_schemas=frozen_schemas,
                session=session,
            )
            log.info(
                "task_tool_schemas_frozen",
                extra={
                    "msg": "Task 创建时已固化主 Agent 工具 schema",
                    "data": {
                        "task_id": task.id,
                        "task_type": task_type,
                        "tool_count": len(frozen_schemas),
                    },
                },
            )
            return task
        task_runtime_spaces.get_or_create(task_id)
        return self._task.get(task_id)

    def collect_task_tree_ids(self, task_id: int) -> set[int]:
        """返回任务及其递归子任务（含 Team 节点）的 id；删除前先停止这些 Task 的执行。"""
        return self._collect_task_tree_ids(task_id)

    def list_task_ids_to_dispatch(self) -> set[int]:
        """启动时需要拉起 actor 的 Task：外部队列有待处理消息，或当前 Run 挂起等唤醒。

        进程重启后 actor 不会自动恢复：这些 Task 若没人访问，已到达的消息不会被处理、挂起的 Run
        也等不到唤醒。遗留 running Run 已由恢复流程收敛为 cancelled，不在此列。
        """
        from app.models.enums.conversation_run_status import ConversationRunStatus
        from app.storage.crud.task_message_crud import TaskMessageCrud

        with self._session_factory() as session:
            pending = TaskMessageCrud().list_task_ids_with_pending(session)
            suspended = self._task.list_ids_with_current_run_status(
                ConversationRunStatus.SUSPENDED.value, session=session
            )
        return {*pending, *suspended}

    def list_child_tasks(self, task_id: int) -> list[TaskRecord]:
        """返回由该任务直接委派的子任务（不含 Agent Team 节点任务）。"""
        return self._task.list_by_parent_task(task_id)

    def require_current_run(self, task_id: int) -> ConversationRunRecord | None:
        """返回存在的任务的当前 Run（Task 的 ``current_run_id`` 指针）；任务尚无 Run 时返回 ``None``。

        调度入口要让不存在的 Task 以 ``KeyError`` 上浮（接入层映射为 404），不能把「Task 不存在」
        误读成「尚无 Run」，所以 Task 必须先读到、不吞异常。

        异常:
            KeyError: 如果指定任务不存在。
        """

        task = self._task.get(task_id)
        # 新建的 Task 还没有 Run：按 ``None`` 去取 Run 会得到 ``KeyError(None)``，被接入层误报为
        # 「task not found」。
        if task.current_run_id is None:
            return None
        return self._turn.get(task.current_run_id)

    def get_task(self, task_id: int) -> TaskRecord:
        """按标识取单个任务，并附带派生的执行态。

        参数:
            task_id: 任务标识。

        返回:
            对应的 ``TaskRecord``，其 ``execution_status`` 从 ``current_run_id`` 指向的 Run 投影；
            无当前 Run 时为空，不维护额外状态机。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task_reads.get(task_id)

    def update_title(self, task_id: int, title: str) -> None:
        """写入 Task 标题；不进 Task 闸门：单列覆盖，与 Run 调度、删除没有需要互斥的不变量。

        异常:
            KeyError: Task 不存在（含已被删除）。
        """
        self._task.update_title(task_id, title)

    async def fork_task(
        self,
        source_task_id: int,
        *,
        user_message_id: int | None,
        leading_run_id: int | None,
        source_message_id: str,
    ) -> TaskRecord:
        """在源 Task 的某一轮回复处创建一个独立的 fork Task（规则见 :meth:`_copy_fork_history`）。

        「一轮」以 ``user_message_id`` 那条用户消息开头，或是 ``leading_run_id`` 那个 Run 开头、
        没有用户输入的回复；二者恰给一个。``source_message_id`` 只记进 fork 来源供追溯。
        """

        supervisor = asyncio.create_task(
            asyncio.to_thread(
                self._fork_task_in_workspace,
                source_task_id,
                _ForkPoint(user_message_id, leading_run_id, source_message_id),
            )
        )
        try:
            return await asyncio.shield(supervisor)
        except asyncio.CancelledError:
            # 持锁与 SQLite worker 属于 supervisor，而不是 HTTP 请求协程；请求
            # 被重复取消时仍由 supervisor 完成事务并负责释放 workspace 操作闸门。
            supervisor.add_done_callback(self._consume_detached_fork_result)
            raise

    @staticmethod
    def _consume_detached_fork_result(task: asyncio.Task[TaskRecord]) -> None:
        """消费被取消请求遗留的 supervisor 结果，避免后台异常无人观察。"""

        if task.cancelled():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            log.error(
                "task_fork_detached_worker_failed",
                extra={
                    "msg": "已取消请求的 fork supervisor 执行失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )

    def _fork_task_in_workspace(self, source_task_id: int, point: _ForkPoint) -> TaskRecord:
        """在指定的一轮回复处创建一个独立的 fork Task。

        workspace 闸门协调结构性删除；源 Task 不加锁，也不访问运行时 context。
        状态校验与全部历史复制在同一个 BEGIN IMMEDIATE 事务内完成。Run 的
        新主键通过 ``ConversationRunCrud.clone_for_task`` 生成，再用映射表改写 context；
        不触发普通 Run 创建事件；执行进度不随历史分叉复制。

        参数:
            source_task_id: 被 fork 的源 Task 标识。
            point: 切分点（一轮回复），复制到这一轮的最后一条 context 为止。

        返回:
            已提交的目标 fork Task。

        异常:
            KeyError: 源 Task 不存在，或切分点不对应源 Task 中可见的一轮回复。
            TaskForkConflictError: 复制前缀存在非终态 Run，或 workspace 操作忙碌。
        副作用:
            新增目标 Task、历史 cloned Runs 与 context entries；源 Task 不变。
        """

        source = self._task.get(source_task_id)
        try:
            with workspace_operations.operation(source.workspace_id, timeout=10):
                return self._copy_fork_history(source_task_id, point)
        except TimeoutError as exc:
            raise TaskForkConflictError(
                "WORKSPACE_BUSY", "workspace has an active structural operation"
            ) from exc

    def _copy_fork_history(self, source_task_id: int, point: _ForkPoint) -> TaskRecord:
        """持有 workspace 闸门时复制数据库历史；后续 Run 的状态不影响选定前缀。

        复制范围：切分点所在那一轮的最后一条 context 及之前的全部可见 context，以及拥有这些
        context 的 Run；切分点所在 Run 只带到这一轮为止（同一 Run 里更晚的轮次不复制）。前缀里的
        Run 都须已到终态。BEGIN IMMEDIATE 在读取之前取得写锁，确保检查与复制之间不会插入 resume、
        编辑或删除提交。提交后只记录汇总日志，不做数据库回读或运行时预加载。
        """

        with begin_immediate(self._session_factory) as session:
            source = self._task.ensure_task(session, source_task_id)
            runs = self._turn.list_by_task_in_session(session, source_task_id)
            rows = self._context.history(source_task_id, session)
            boundary_run_id, cut_sequence = _resolve_fork_cut(rows, point)
            boundary_index = next(
                (index for index, run in enumerate(runs) if run.id == boundary_run_id),
                None,
            )
            if boundary_index is None:
                raise KeyError(boundary_run_id)
            source_prefix = runs[: boundary_index + 1]
            if any(run.status not in TERMINAL_RUN_STATUSES for run in source_prefix):
                raise TaskForkConflictError(
                    "RUN_NOT_READY",
                    "all runs in the selected history prefix must be terminal before forking",
                )

            target = self._task.create(
                workspace_id=source.workspace_id,
                title=self._task.next_fork_title(source.workspace_id, source.title, session),
                task_type="fork",
                extra={
                    "fork": {
                        "source_task_id": source_task_id,
                        "source_message_id": point.source_message_id,
                    }
                },
                tool_schemas=source.tool_schemas,
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
                max_sequence=cut_sequence,
            )
            # 当前 Run 指向克隆出的切分点 Run：snapshot 的 activity 由它投影，没有指针时前端拿不到
            # 终态，编辑重跑等「静止后才开放」的入口不会出现；新 Run 也要从它继承发起配置。
            target = self._task.set_current_run_id(
                target.id, run_id_map[boundary_run_id], session=session
            )
        log.info(
            "task_forked",
            extra={
                "msg": "task fork committed",
                "data": {
                    "source_task_id": source_task_id,
                    "boundary_run_id": boundary_run_id,
                    "source_message_id": point.source_message_id,
                    "target_task_id": target.id,
                    "cloned_run_count": len(run_id_map),
                    "cloned_context_count": cloned_context_count,
                },
            },
        )
        # target 已经在事务内 flush，且提交后的只读查询不是 Fork 成功条件；
        # 直接返回事务内记录，避免“已提交但最后 get 失败”伪装成 Fork 失败。
        return target

    def list_tasks_for_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """列出某工作区下的用户任务（排除委派子任务）。

        委派子任务（``task_type='delegate_task'``）不进侧边栏对话列表，故本方法仅返回
        ``task_type='user'`` 或 ``'fork'`` 的任务，按更新时间倒序。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区下的用户任务记录列表（不含委派子任务）；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task_reads.list_by_workspace(workspace_id)

    async def delete_task_tree(self, task_id: int, *, with_forks: bool = False) -> frozenset[int]:
        """删除 Task 的用户用例：删除 = 停止 + 删除记录，停止在任何锁之外；返回实际删除的 Task id。

        先停下所有权子树（沿 ``parent_task_id`` 的委派子 Agent 与 Team 节点）的 actor 并等执行
        落定，停止即墓碑（见 ``TaskRuntimeSpaceRegistry.stop``）；收集与停止反复到子树不再增长，
        覆盖父 Agent 在停下前刚委派出的子 Task。再由 :meth:`delete_task` 在一个事务里删除记录。
        fork 不在所有权子树里，默认不随源 Task 删除、只改挂（见 :meth:`delete_task_tree_in_session`）；
        ``with_forks`` 为真时把全部 fork 后代（含 fork 的 fork）当作额外的根一并停止和删除。fork 后代
        只在开头收集一次，删除的就是停下的这批：停止之后才 fork 出来的照常改挂，不会删掉没停下的 Task。

        删除没成功（闸门忙、写库失败）时为已停下的 Task 换上新 actor：Task 仍在，不能留着没有
        调度者。请求被取消时不回滚——工作线程里的删除可能照常提交，回滚会给已删除的 Task 拉起 actor。

        异常:
            KeyError: Task 不存在。
            DeletionBusyError: workspace 正被其他结构性操作占用。
        """

        fork_ids = (
            await asyncio.to_thread(self.collect_fork_descendant_ids, task_id)
            if with_forks
            else frozenset()
        )
        stopped: set[int] = set()
        try:
            while True:
                task_ids: set[int] = set()
                for root_id in (task_id, *fork_ids):
                    task_ids |= await asyncio.to_thread(self.collect_task_tree_ids, root_id)
                if task_ids <= stopped:
                    break
                await self._task_register.stop(task_ids - stopped)
                stopped |= task_ids
            return await asyncio.to_thread(self.delete_task, task_id, fork_ids)
        except Exception:
            self._task_register.restart_stopped(stopped)
            raise

    def collect_fork_descendant_ids(self, task_id: int) -> frozenset[int]:
        """返回从 ``task_id`` fork 出的全部后代（fork 的 fork 也算），不含自身。

        fork 来源记在 ``extra.fork.source_task_id``（不是 ``parent_task_id``），所以按工作区读出全部
        fork 的来源再从根向下展开。来源关系异常成环时，已访问的不重复展开。

        异常:
            KeyError: Task 不存在。
        """
        workspace_id = self._task.get(task_id).workspace_id
        children: dict[int, list[int]] = {}
        for fork_id, source_id in self._task.list_fork_sources(workspace_id).items():
            children.setdefault(source_id, []).append(fork_id)
        found: set[int] = set()
        pending = [task_id]
        while pending:
            for child_id in children.get(pending.pop(), []):
                if child_id != task_id and child_id not in found:
                    found.add(child_id)
                    pending.append(child_id)
        return frozenset(found)

    def delete_task(self, task_id: int, fork_ids: frozenset[int] = frozenset()) -> frozenset[int]:
        """原子删除任务树并级联清理其下全部子产物；调用方须已停下这棵树的 actor。

        删除前先校验任务存在（不存在则抛 ``KeyError``），再取得 workspace 闸门（与 fork、删除
        Run、删除 workspace 互斥）。不取各 Task 的闸门：它们只仲裁 actor 的调度轮次，actor 已在
        :meth:`delete_task_tree` 里停下。在单个 ``BEGIN IMMEDIATE`` 事务内按「子任务先于父任务」
        的后序清理每个任务自身及其产物，保证原子性（全删或全不删）。
        提交后卸载进程内 runtime space。Task 删除不负责
        删除图片附件，也不触发 workspace 附件孤儿 GC。删除是高风险操作，保留 start / complete
        审计日志。

        参数:
            task_id: 待删除的任务标识。
            fork_ids: 一并删除的 fork 后代（各自连同所有权子树），由 :meth:`delete_task_tree` 停下。

        返回:
            实际删除的全部 Task id（含各棵所有权子树）。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败（事务回滚）。

        副作用:
            从 ``conversation_task_contexts`` / ``conversation_runs`` / ``tasks`` 表删除该任务树
            相关数据；并提交后清理已删任务遗留的
            运行期缓存并卸载进程内 runtime space。
        """

        task = self._task.get(task_id)
        try:
            with workspace_operations.operation(task.workspace_id, timeout=10):
                result = self._delete_task_locked(task_id, fork_ids)
        except TimeoutError as exc:
            raise DeletionBusyError("task", task_id) from exc
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {
                    "task_id": task_id,
                    "fork_ids": sorted(fork_ids),
                    "deleted_tasks": len(result.task_ids),
                },
            },
        )
        return result.task_ids

    def cleanup_provisional_task(self, task_id: int, creation_command_id: str) -> None:
        """在 Assistant Transport 尚未接受首条命令时受控删除 provisional Task。

        只有任务创建标记完全匹配、任务树尚未产生任何 Run 时才允许删除。该用例复用普通
        Task 树删除核心，但明确不做图片附件删除或 workspace 附件孤儿 GC；清理失败由调用方
        保留原始 Transport 错误并记录诊断日志。
        """

        if not creation_command_id.strip():
            raise ProvisionalTaskCleanupConflictError("creationCommandId is required")
        task = self._task.get(task_id)
        if task.creation_command_id != creation_command_id:
            raise ProvisionalTaskCleanupConflictError("creationCommandId does not match task")
        try:
            with workspace_operations.operation(task.workspace_id, timeout=10):
                current = self._task.get(task_id)
                if current.creation_command_id != creation_command_id:
                    raise ProvisionalTaskCleanupConflictError(
                        "creationCommandId does not match task"
                    )
                tree_ids = self._collect_task_tree_ids(task_id)
                locked_ids = self._collect_task_ancestor_ids(task_id) | tree_ids
                with ExitStack() as stack:
                    for current_id in sorted(locked_ids):
                        stack.enter_context(
                            self._task_register.get_or_create(current_id).operation(timeout=10)
                        )
                    if any(self._turn.list_by_task(current_id) for current_id in tree_ids):
                        raise ProvisionalTaskCleanupConflictError(
                            "provisional task already has accepted business facts"
                        )
                    self._delete_task_after_operations(task_id)
        except TimeoutError as exc:
            raise DeletionBusyError("task", task_id) from exc
        log.info(
            "provisional_task_cleaned",
            extra={
                "msg": "provisional task cleaned without attachment deletion",
                "data": {"task_id": task_id, "creation_command_id": creation_command_id},
            },
        )

    def _delete_task_locked(self, task_id: int, fork_ids: frozenset[int]) -> TaskDeletionResult:
        """在已持有 workspace 闸门时删除任务树（及给定的 fork 后代），不触发附件清理。"""

        from app.core.tool.handler.agent_team.coordinator import get_agent_team_coordinator

        tree_ids: set[int] = set()
        for root_id in (task_id, *fork_ids):
            tree_ids |= self._collect_task_tree_ids(root_id)
        get_agent_team_coordinator().cancel_for_task_ids(tree_ids)
        return self._delete_task_after_operations(task_id, fork_ids)

    def _delete_task_after_operations(
        self, task_id: int, fork_ids: frozenset[int] = frozenset()
    ) -> TaskDeletionResult:
        """在已持有 workspace 闸门时提交任务树删除。"""

        log.info(
            "task_delete_start",
            extra={
                "msg": "task delete started",
                "data": {"task_id": task_id, "fork_ids": sorted(fork_ids)},
            },
        )
        with begin_immediate(self._session_factory) as session:
            result = self.delete_task_tree_in_session(task_id, session, fork_ids=fork_ids)
        self.finalize_deleted_task_spaces(result)
        return result

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

    def _collect_task_tree_ids(self, root_task_id: int) -> set[int]:
        """收集任务树（根任务及沿 ``parent_task_id`` 递归的全部子 Task）的 id 集合。"""

        with self._session_factory() as session:
            return set(self._collect_task_tree_ids_postorder(root_task_id, session))

    def _collect_task_tree_ids_postorder(self, root_task_id: int, session: Session) -> list[int]:
        """按「子任务先于父任务」的顺序返回任务树全部 id。

        ``tasks.parent_task_id`` 是即时检查的自引用外键，父行必须晚于全部子行删除。广度优先
        发现顺序里父任务总在子任务之前，反转即得所需顺序。子任务按 ``list_child_ids`` 收集、
        不区分类型：Team 节点 Task 同样挂在父任务下，漏掉会让删除父行时外键失败。
        """

        order = [root_task_id]
        index = 0
        while index < len(order):
            order.extend(self._task.list_child_ids(order[index], session))
            index += 1
        return order[::-1]

    def delete_task_tree_in_session(
        self, root_task_id: int, session: Session, *, fork_ids: frozenset[int] = frozenset()
    ) -> TaskDeletionResult:
        """按后序在同一事务删除所有权子树，返回提交后需卸载的 Task 集合。

        所有权子树沿 ``parent_task_id``（委派子 Agent、Team 节点）级联删除：它们是被删 Agent 执行的
        一部分，提升到上一级会把子 Agent 交给没有创建它的 Agent（``child_agent_*`` 按
        ``parent_task_id`` 授权、重启收尾按它回投结论）。fork 是独立对话，来源在 ``extra.fork``，
        不在子树里：``fork_ids`` 里的 fork 作为额外的根连同各自子树一起删，其余来源被删的 fork 改挂到
        被删根的来源（根不是 fork 时成为根），侧栏的 fork 树因此只少一层。fork 之间没有外键，
        各棵子树内部仍按后序删除即可。
        """

        task_ids_postorder = [
            current_id
            for tree_root_id in (root_task_id, *sorted(fork_ids))
            for current_id in self._collect_task_tree_ids_postorder(tree_root_id, session=session)
        ]
        deleted = frozenset(task_ids_postorder)
        root = self._task.ensure_task(session, root_task_id)
        self._task.reparent_forks(root.workspace_id, deleted, _fork_source(root, deleted), session)
        for current_id in task_ids_postorder:
            self._delete_single_task_in_session(current_id, session)
        return TaskDeletionResult(
            task_ids=frozenset(task_ids_postorder),
        )

    def finalize_deleted_task_spaces(self, result: TaskDeletionResult) -> None:
        """数据库提交后回收 Task 级资源（终端、延迟结果、看板）与运行时空间；回滚路径一概不碰。

        延迟结果归 Task、比 Run 活得久，必须在这里撤销而不是指望删除前的停止：停止只在有执行者时
        撤销，空闲（Run 已完成、后台命令还在跑）或挂起（等后台结论）的 Task 没有执行者。撤销会
        终止后台命令；占位行已随删除消失，不结算。
        """
        from app.core.tool.background.deferred_registry import deferred_registry

        service_depends.get_terminal_session_service().delete_task_sessions(result.task_ids)
        for task_id in result.task_ids:
            deferred_registry.drop_task(task_id)
            deferred_registry.forget_task(task_id)
        self._unload_deleted_task_spaces(set(result.task_ids))

    def _unload_deleted_task_spaces(self, task_ids: set[int]) -> None:
        """清除已删除任务的进程内 runtime space 与 context 引用。"""

        for current_id in task_ids:
            try:
                self._task_register.mark_deleted(current_id)
                self._state.mark_task_deleted(current_id)
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

    def _delete_single_task_in_session(self, task_id: int, session: Session) -> None:
        """在调用方事务内删除 Task 与全部子产物；外键级联删除未消费 message_queue。"""

        self._task.ensure_task(session, task_id)  # 存在性守卫
        run_ids = self._turn.collect_run_ids_by_task_ids(session, [task_id])

        self._task_context_crud.delete_by_task_ids([task_id], session)
        self._turn.delete_by_ids(run_ids, session)
        self._task.delete_by_ids([task_id], session)


def _fork_source(task: TaskRecord, deleted: frozenset[int]) -> int | None:
    """被删根的 fork 来源（来源也在删除集里时视为无）：它的 fork 改挂到这里。"""
    if task.task_type != "fork":
        return None
    source = (task.extra or {}).get("fork", {}).get("source_task_id")
    return source if isinstance(source, int) and source not in deleted else None


def _resolve_fork_cut(
    rows: list[ConversationTaskContextRecord], point: _ForkPoint
) -> tuple[int, int]:
    """把切分点换算成 ``(所在 Run, 这一轮最后一条 context 的序号)``；只看前端可见的行。

    一轮从它的用户消息（或 Run 开头）起，到同一 Run 的下一条用户输入之前为止。

    异常:
        KeyError: 切分点在源 Task 的可见历史里找不到。
    """

    visible = sorted(
        (row for row in rows if row.run_id is not None and row.project_to_transport),
        key=lambda row: row.sequence,
    )

    def starts_turn(row: ConversationTaskContextRecord) -> bool:
        return (
            row.message["role"] == "user"
            and isinstance(row.metadata, UserMessageMetadata)
            and row.metadata.kind == "user_input"
        )

    if point.user_message_id is not None:
        start = next(
            (
                row
                for row in visible
                if starts_turn(row) and row.message_id == point.user_message_id
            ),
            None,
        )
        if start is None:
            raise KeyError(point.source_message_id)
        run_id = cast(int, start.run_id)
        turn: list[ConversationTaskContextRecord] = []
        for row in visible:
            if row.run_id != run_id or row.sequence < start.sequence:
                continue
            if turn and starts_turn(row):
                break
            turn.append(row)
    else:
        run_id = cast(int, point.leading_run_id)
        turn = []
        for row in visible:
            if row.run_id != run_id:
                continue
            if starts_turn(row):
                break
            turn.append(row)
        if not turn:
            raise KeyError(point.source_message_id)
    return run_id, turn[-1].sequence
