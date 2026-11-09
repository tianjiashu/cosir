"""Task 用例：创建（固化工具 schema）、分叉、删除，以及当前 Run 与派生树的读取。

- 分叉在工作区闸门内一个写事务里完成，切点、初始配置与历史复制的规则在 ``ForkSource``；
- 删除 = 停止 + 删除记录：停止协议在 :meth:`TaskService.stop_then_delete`（工作区删除共用），
  记录在一个事务里按后序级联删除，提交后回收 Task 级资源与 runtime space；
- 不负责 Run 的调度与执行（``core/task_actor.py``），也不直接写 SQL（经各 CRUD）。
"""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from copy import deepcopy
from typing import TypeVar, cast

from sqlalchemy.orm.session import Session

from app.config.logging.logger import log
from app.core.workflow.message_queue.message_bodies import ADVANCING_KINDS
from app.models import ConversationRunRecord, TaskRecord
from app.models.errors.deletion_errors import (
    DeletionBusyError,
    ProvisionalTaskCleanupConflictError,
)
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.models.task_extra import TaskExtra, TaskFork
from app.models.task_kind import FORK_TASK_TYPES, USER_FACING_TASK_TYPES, TaskType
from app.service import depends as service_depends
from app.service.task.fork_source import ForkCut, ForkSource
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.task_runtime.workspace_operation_registry import workspace_operations

_T = TypeVar("_T")

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
        self._state = service_depends.get_conversation_task_state_service()
        self._task_context_crud = service_depends.get_conversation_task_context_crud()
        self._session_factory = main_session_factory()

    def create_task(
        self,
        workspace_id: int,
        title: str,
        *,
        creation_command_id: str | None = None,
        task_type: TaskType = TaskType.USER,
        parent_task_id: int | None = None,
        session: Session | None = None,
        extra: TaskExtra | None = None,
        tool_schemas: list[dict[str, object]] | None = None,
        derivation_depth: int = 0,
    ) -> TaskRecord:
        """创建 Task 并固化它的工具 schema。

        schema 由调用方筛选后传入、本方法不自行推导：主 Agent Task 的由
        ``task_tool_scope.main_agent_tool_schemas`` 解析，委派 / Team 节点由各自调用方筛选。传入
        ``None`` 持久化为空列表，即该 Task 运行期没有可调用工具。

        参数:
            creation_command_id: provisional Task 的创建命令标识。
            parent_task_id: 委派子 Task 的父 Task 标识。
            session: 传入时加入调用方事务。
            derivation_depth: 派生深度（见 ``core.tool.derivation.child_depth``）。
        """

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
            derivation_depth=derivation_depth,
        )
        log.info(
            "task_tool_schemas_frozen",
            extra={
                "msg": "Task 创建时已固化工具 schema",
                "data": {
                    "task_id": task.id,
                    "task_type": task_type,
                    "derivation_depth": derivation_depth,
                    "tool_count": len(frozen_schemas),
                },
            },
        )
        return task

    def list_task_ids_to_dispatch(self) -> set[int]:
        """启动时需要拉起 actor 的 Task：外部队列有能推进的消息，或当前 Run 挂起等唤醒。

        进程重启后 actor 不会自动恢复：这些 Task 若没人访问，已到达的消息不会被处理、挂起的 Run
        也等不到唤醒。遗留 running Run 已由恢复流程收敛为 cancelled，不在此列；只有通知的 Task
        也不在此列——通知不推进 Run，随它下次被驱动时并入（见 ``ADVANCING_KINDS``）。
        """
        from app.models.enums.conversation_run_status import ConversationRunStatus
        from app.storage.crud.task_message_crud import TaskMessageCrud

        with self._session_factory() as session:
            pending = TaskMessageCrud().list_task_ids_with_pending(session, kinds=ADVANCING_KINDS)
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
        """按标识取单个任务。

        参数:
            task_id: 任务标识。

        返回:
            对应的 ``TaskRecord``；执行状态不在这里，见 ``TaskStatusFeed``。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task.get(task_id)

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
        turn: int,
    ) -> TaskRecord:
        """从一条回复（按它所在的回合 ``turn``）分叉：新 Task 继承到这一回合结尾为止的上下文。"""

        supervisor = asyncio.create_task(
            asyncio.to_thread(
                self._fork_at_turn,
                source_task_id,
                turn,
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

    def _fork_at_turn(self, source_task_id: int, turn: int) -> TaskRecord:
        """手动分叉：切在所选回复这一回合的结尾（规则见 ``ForkSource.cut_after_turn``）。"""
        return self._fork(
            source_task_id,
            lambda source: source.cut_after_turn(turn),
            title=None,
            task_type=TaskType.USER_FORK,
            depth=0,
        )

    def fork_for_agent(
        self,
        source_task_id: int,
        *,
        run_id: int,
        tool_call_id: str,
        title: str,
        derivation_depth: int,
    ) -> TaskRecord:
        """切在调用所在回合的开头：继承到上一条回复，当前回合交给新的任务说明。"""
        return self._fork(
            source_task_id,
            lambda source: source.cut_before_call(run_id, tool_call_id),
            title=title,
            task_type=TaskType.AGENT_FORK,
            depth=derivation_depth,
        )

    def _fork(
        self,
        source_task_id: int,
        cut: Callable[[ForkSource], ForkCut],
        *,
        title: str | None,
        task_type: TaskType,
        depth: int,
    ) -> TaskRecord:
        """两种分叉共用的编排：工作区闸门内一个写事务里定切点、建目标 Task、复制切点之前的历史。

        ``cut`` 在来源上取切点与初始配置，两种分叉只在这里不同。切点在写事务内算：点击到提交之间
        并入的新输入不会切错位置。``title`` 为 ``None`` 时按来源标题编号。

        异常:
            TaskForkConflictError: 来源不能分叉，或工作区有其他结构性操作（``WORKSPACE_BUSY``）。
            ``cut`` 抛出的异常原样上浮。
        """
        source = self._task.get(source_task_id)
        try:
            with (
                workspace_operations.operation(source.workspace_id, timeout=10),
                begin_immediate(self._session_factory) as session,
            ):
                source = self._task.ensure_task(session, source_task_id)
                _require_forkable(source)
                fork_source = ForkSource(
                    task=source,
                    rows=self._task_context_crud.get(
                        source_task_id, include_in_context=False, session=session
                    ),
                    runs=self._turn.list_by_task_in_session(session, source.id),
                )
                fork_cut = cut(fork_source)
                target = self._task.create(
                    workspace_id=source.workspace_id,
                    title=title or self._task.next_fork_title(
                        source.workspace_id, source.title, session
                    ),
                    task_type=task_type,
                    extra=TaskExtra(fork=TaskFork(
                        source_task_id=source.id,
                        source_title=source.title,
                        tree_parent_task_id=source.id,
                        initial_launch=fork_cut.launch,
                    )),
                    # 工具列表是请求前缀的一部分，改了就接不上源的缓存；派生限制由运行期准入执行。
                    tool_schemas=deepcopy(source.tool_schemas),
                    session=session,
                    derivation_depth=depth,
                )
                count = fork_source.copy_before(fork_cut, target, self._task_context_crud, session)
        except TimeoutError as exc:
            raise TaskForkConflictError(
                "WORKSPACE_BUSY", "workspace has an active structural operation"
            ) from exc
        log.info(
            "task_fork_copied",
            extra={
                "msg": "copied context before the turn boundary",
                "data": {
                    "source_task_id": source.id,
                    "target_task_id": target.id,
                    "before_sequence": fork_cut.before_sequence,
                    "task_type": task_type,
                    "context_count": count,
                },
            },
        )
        return target

    def list_tasks_for_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """列出某工作区下的用户任务（排除委派子任务）。

        委派子任务（``task_type='agent_task'``）不进侧边栏对话列表，故本方法仅返回
        ``user``、``user_fork`` 或 ``agent_fork`` 的任务，按更新时间倒序。

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

    async def delete_task_tree(self, task_id: int, *, with_forks: bool = False) -> frozenset[int]:
        """删除 Task 的用户用例：删除 = 停止 + 删除记录，停止在任何锁之外；返回实际删除的 Task id。

        经 :meth:`stop_then_delete` 先停下所有权子树（沿 ``parent_task_id`` 的委派子 Agent 与 Team
        节点），再由 :meth:`delete_task` 在一个事务里删除记录。
        fork 不在所有权子树里，默认不随源 Task 删除、只改挂（见 :meth:`delete_task_tree_in_session`）；
        ``with_forks`` 为真时把全部 fork 后代（含 fork 的 fork）当作额外的根一并停止和删除。fork 后代
        只在开头收集一次，删除的就是停下的这批：停止之后才 fork 出来的照常改挂，不会删掉没停下的 Task。

        异常:
            KeyError: Task 不存在。
            DeletionBusyError: workspace 正被其他结构性操作占用。
        """

        fork_ids = (
            await asyncio.to_thread(self.collect_fork_descendant_ids, task_id)
            if with_forks
            else frozenset()
        )
        def collect() -> set[int]:
            task_ids: set[int] = set()
            for root_id in (task_id, *fork_ids):
                task_ids |= self.collect_task_tree_ids(root_id)
            return task_ids

        return await self.stop_then_delete(collect, lambda: self.delete_task(task_id, fork_ids))

    async def stop_then_delete(
        self, collect: Callable[[], set[int]], delete: Callable[[], _T]
    ) -> _T:
        """删除一批 Task 的统一协议：在任何锁之外停下 ``collect()`` 给出的 Task，再执行 ``delete``。

        停止即墓碑（见 ``TaskRuntimeSpaceRegistry.stop``），并等各 Task 的 Team 协调任务退出；收集与
        停止反复到集合不再增长，覆盖父 Agent 在停下前刚委派出的子 Task。``collect`` 与 ``delete`` 是
        同步函数，在工作线程里调用。``delete`` 没成功（闸门忙、写库失败）时为已停下的 Task 换上新
        actor：Task 仍在，不能留着没有调度者。请求被取消时不回滚——工作线程里的删除可能照常提交，
        回滚会给已删除的 Task 拉起 actor。
        """
        stopped: set[int] = set()
        try:
            while True:
                task_ids = await asyncio.to_thread(collect)
                if task_ids <= stopped:
                    break
                await self._task_register.stop(task_ids - stopped)
                await service_depends.get_agent_team_coordinator().drain_for_deletion(task_ids)
                stopped |= task_ids
            return await asyncio.to_thread(delete)
        except Exception:
            self._task_register.restart_stopped(stopped)
            raise

    def collect_fork_descendant_ids(self, task_id: int) -> frozenset[int]:
        """返回从 ``task_id`` fork 出的全部后代（fork 的 fork 也算），不含自身。

        fork 树关系记在 ``extra.fork.tree_parent_task_id``（不是 ``parent_task_id``），所以按工作区读出全部
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
                deleted = self._delete_task_locked(task_id, fork_ids)
        except TimeoutError as exc:
            raise DeletionBusyError("task", task_id) from exc
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {
                    "task_id": task_id,
                    "fork_ids": sorted(fork_ids),
                    "deleted_tasks": len(deleted),
                },
            },
        )
        return deleted

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
                tree_ids = self.collect_task_tree_ids(task_id)
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

    def _delete_task_locked(self, task_id: int, fork_ids: frozenset[int]) -> frozenset[int]:
        """在已持有 workspace 闸门时删除任务树（及给定的 fork 后代），不触发附件清理。"""

        tree_ids: set[int] = set()
        for root_id in (task_id, *fork_ids):
            tree_ids |= self.collect_task_tree_ids(root_id)
        service_depends.get_agent_team_coordinator().cancel_for_task_ids(tree_ids)
        return self._delete_task_after_operations(task_id, fork_ids)

    def _delete_task_after_operations(
        self, task_id: int, fork_ids: frozenset[int] = frozenset()
    ) -> frozenset[int]:
        """在已持有 workspace 闸门时提交任务树删除。"""

        log.info(
            "task_delete_start",
            extra={
                "msg": "task delete started",
                "data": {"task_id": task_id, "fork_ids": sorted(fork_ids)},
            },
        )
        with begin_immediate(self._session_factory) as session:
            deleted = self.delete_task_tree_in_session(task_id, session, fork_ids=fork_ids)
        self.finalize_deleted_task_spaces(deleted)
        return deleted

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

    def collect_task_tree_ids(self, root_task_id: int) -> set[int]:
        """收集任务树（根任务及沿 ``parent_task_id`` 递归的全部子 Task，含 Team 节点）的 id 集合；
        删除前先停止这些 Task 的执行。"""

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
    ) -> frozenset[int]:
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
        return deleted

    def finalize_deleted_task_spaces(self, task_ids: frozenset[int]) -> None:
        """数据库提交后回收 Task 级资源（终端、浏览器标签、后台结果、看板）与运行时空间；回滚路径
        一概不碰。

        后台结果归 Task、比 Run 活得久，必须在这里撤销而不是指望删除前的停止：停止只在有执行者时
        撤销，空闲（Run 已完成、后台命令还在跑）或挂起（等后台结论）的 Task 没有执行者。撤销会
        终止后台命令；占位行已随删除消失，不结算。
        """
        from app.core.tool.background.background_registry import background_registry
        from app.core.tool.handoff.handoff_registry import handoff_registry

        service_depends.get_terminal_session_service().delete_task_sessions(task_ids)
        # 浏览器标签页归宿主所有：发后即忘的通知，宿主未连接时标签页也已不在（链路记日志）。
        service_depends.get_host_link().notify(
            "browser.dropTasks", {"task_ids": sorted(task_ids)}
        )
        for task_id in task_ids:
            background_registry.drop_task(task_id)
            handoff_registry.drop_task(task_id)
        self._unload_deleted_task_spaces(set(task_ids))

    def _unload_deleted_task_spaces(self, task_ids: set[int]) -> None:
        """清除已删除任务的进程内 runtime space 与 context 引用。"""

        for current_id in task_ids:
            try:
                self._task_register.mark_deleted(current_id)
                self._state.release_deleted_task(current_id)
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
    if task.task_type not in FORK_TASK_TYPES:
        return None
    source = cast(TaskFork, task.extra.fork).tree_parent_task_id
    return source if source not in deleted else None


def _require_forkable(source: TaskRecord) -> None:
    """只有侧栏对话能 fork：子 Agent 与 Team 节点的工具面、系统提示词都是按只读 Task 定的，复制成
    侧栏对话会得到身份与能力对不上的混合体（前端只读面本来就不给 fork 入口）。"""
    if source.task_type not in USER_FACING_TASK_TYPES:
        raise TaskForkConflictError(
            "FORK_SOURCE_UNSUPPORTED",
            "only sidebar conversations can be forked",
            retryable=False,
        )
