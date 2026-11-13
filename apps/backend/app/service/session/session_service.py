"""Session 用例：创建（固化工具 schema）、分叉、删除，以及当前 Run 与派生树的读取。

- 分叉在工作区闸门内一个写事务里完成，切点、初始配置与历史复制的规则在 ``ForkSource``；
- 删除 = 停止 + 删除记录：停止协议在 :meth:`SessionService.stop_then_delete`（工作区删除共用），
  记录在一个事务里按后序级联删除，提交后回收 Session 级资源与 runtime space；
- 不负责 Run 的调度与执行（``core/session_actor.py``），也不直接写 SQL（经各 CRUD）。
"""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from copy import deepcopy
from typing import TypeVar, cast

from sqlalchemy.orm.session import Session as DbSession

from app.config.logging.logger import log
from app.core.workflow.message_queue.message_bodies import ADVANCING_KINDS
from app.models import RunRecord, SessionRecord
from app.models.enums.run_status import ACTIVE_RUN_STATUSES
from app.models.errors.deletion_errors import (
    DeletionBusyError,
    ProvisionalSessionCleanupConflictError,
)
from app.models.errors.session_fork_errors import SessionForkConflictError
from app.models.session_extra import SessionExtra, SessionFork
from app.models.session_kind import FORK_SESSION_TYPES, USER_FACING_SESSION_TYPES, SessionType
from app.service import depends as service_depends
from app.service.session.fork_source import ForkCut, ForkSource
from app.storage.store_engines import main_db_factory
from app.storage.write_transaction import begin_immediate
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.session_runtime.workspace_operation_registry import workspace_operations

_T = TypeVar("_T")

class SessionService:
    """Orchestrate session creation, lifecycle management, and execution-status derivation."""

    def __init__(self) -> None:
        """初始化会话 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._session_register = session_runtime_spaces
        self._session = service_depends.get_session_crud()
        self._turn = service_depends.get_run_crud()
        self._state = service_depends.get_conversation_state_service()
        self._session_context_crud = service_depends.get_session_context_crud()
        self._db_factory = main_db_factory()

    def create_session(
        self,
        workspace_id: int,
        title: str,
        *,
        creation_command_id: str | None = None,
        session_type: SessionType = SessionType.USER,
        parent_session_id: int | None = None,
        db: DbSession | None = None,
        extra: SessionExtra | None = None,
        tool_schemas: list[dict[str, object]] | None = None,
        derivation_depth: int = 0,
    ) -> SessionRecord:
        """创建 Session 并固化它的工具 schema。

        schema 由调用方筛选后传入、本方法不自行推导：主 Agent Session 的由
        ``session_tool_scope.main_agent_tool_schemas`` 解析，委派由调用方筛选。传入
        ``None`` 持久化为空列表，即该 Session 运行期没有可调用工具。

        参数:
            creation_command_id: provisional Session 的创建命令标识。
            parent_session_id: 委派子 Session 的父 Session 标识。
            db: 传入时加入调用方事务。
            derivation_depth: 派生深度（见 ``core.tool.derivation.child_depth``）。
        """

        frozen_schemas = [] if tool_schemas is None else tool_schemas
        session = self._session.create(
            workspace_id=workspace_id,
            title=title,
            session_type=session_type,
            parent_session_id=parent_session_id,
            creation_command_id=creation_command_id,
            extra=extra,
            tool_schemas=frozen_schemas,
            db=db,
            derivation_depth=derivation_depth,
        )
        log.info(
            "session_tool_schemas_frozen",
            extra={
                "msg": "Session 创建时已固化工具 schema",
                "data": {
                    "session_id": session.id,
                    "session_type": session_type,
                    "derivation_depth": derivation_depth,
                    "tool_count": len(frozen_schemas),
                },
            },
        )
        return session

    def list_session_ids_to_dispatch(self) -> set[int]:
        """启动时需要拉起 actor 的 Session：外部队列有能推进的消息，或当前 Run 挂起等唤醒。

        进程重启后 actor 不会自动恢复：这些 Session 若没人访问，已到达的消息不会被处理、挂起的 Run
        也等不到唤醒。遗留 running Run 已由恢复流程收敛为 cancelled，不在此列；只有通知的 Session
        也不在此列——通知不推进 Run，随它下次被驱动时并入（见 ``ADVANCING_KINDS``）。
        """
        from app.models.enums.run_status import RunStatus
        from app.storage.crud.session_message_crud import SessionMessageCrud

        with self._db_factory() as db:
            pending = SessionMessageCrud().list_session_ids_with_pending(db, kinds=ADVANCING_KINDS)
            suspended = self._session.list_ids_with_current_run_status(
                RunStatus.SUSPENDED.value, db=db
            )
        return {*pending, *suspended}

    def list_derived_sessions(self, session_id: int) -> list[SessionRecord]:
        """返回该 Session 直接派生的 Session：委派的子 Agent 与 Agent fork（``deriver_id()`` 指向它的）。

        fork 不挂所有权子树，发起方记在 ``extra.fork``，所以按工作区读出 Agent fork 再过滤。

        异常:
            KeyError: Session 不存在。
        """
        session = self._session.get(session_id)
        forks = [
            fork
            for fork in self._session.list_by_workspace_and_type(session.workspace_id, SessionType.AGENT_FORK)
            if fork.deriver_id() == session_id
        ]
        return [*self._session.list_by_parent_session(session_id), *forks]

    def require_current_run(self, session_id: int) -> RunRecord | None:
        """返回存在的会话的当前 Run（Session 的 ``current_run_id`` 指针）；会话尚无 Run 时返回 ``None``。

        调度入口要让不存在的 Session 以 ``KeyError`` 上浮（接入层映射为 404），不能把「Session 不存在」
        误读成「尚无 Run」，所以 Session 必须先读到、不吞异常。

        异常:
            KeyError: 如果指定会话不存在。
        """

        session = self._session.get(session_id)
        # 新建的 Session 还没有 Run：按 ``None`` 去取 Run 会得到 ``KeyError(None)``，被接入层误报为
        # 「session not found」。
        if session.current_run_id is None:
            return None
        return self._turn.get(session.current_run_id)

    def get_session(self, session_id: int) -> SessionRecord:
        """按标识取单个会话。

        参数:
            session_id: 会话标识。

        返回:
            对应的 ``SessionRecord``；执行状态不在这里，见 ``SessionFeed``。

        异常:
            KeyError: 如果指定会话不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._session.get(session_id)

    def update_title(self, session_id: int, title: str) -> None:
        """写入 Session 标题，真的变了才推送目录失效；不进 Session 闸门：单列覆盖，与 Run 调度、删除没有需要互斥的不变量。

        异常:
            KeyError: Session 不存在（含已被删除）。
        """
        if self._session.update_title(session_id, title):
            service_depends.get_session_feed().publish_session_catalog(session_id)

    async def fork_session(
        self,
        source_session_id: int,
        *,
        turn: int,
    ) -> SessionRecord:
        """从一条回复（按它所在的回合 ``turn``）分叉：新 Session 继承到这一回合结尾为止的上下文。"""

        supervisor = asyncio.create_task(
            asyncio.to_thread(
                self._fork_at_turn,
                source_session_id,
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
    def _consume_detached_fork_result(task: asyncio.Task[SessionRecord]) -> None:
        """消费被取消请求遗留的 supervisor 结果，避免后台异常无人观察。"""

        if task.cancelled():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            log.error(
                "session_fork_detached_worker_failed",
                extra={
                    "msg": "已取消请求的 fork supervisor 执行失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )

    def _fork_at_turn(self, source_session_id: int, turn: int) -> SessionRecord:
        """手动分叉：切在所选回复这一回合的结尾（规则见 ``ForkSource.cut_after_turn``）。"""
        return self._fork(
            source_session_id,
            lambda source: source.cut_after_turn(turn),
            title=None,
            session_type=SessionType.USER_FORK,
            depth=0,
        )

    def fork_for_agent(
        self,
        source_session_id: int,
        *,
        run_id: int,
        tool_call_id: str,
        title: str,
        derivation_depth: int,
    ) -> SessionRecord:
        """切在调用所在回合的开头：继承到上一条回复，当前回合交给新的任务说明。"""
        return self._fork(
            source_session_id,
            lambda source: source.cut_before_call(run_id, tool_call_id),
            title=title,
            session_type=SessionType.AGENT_FORK,
            depth=derivation_depth,
            deriving_call_id=tool_call_id,
        )

    def _fork(
        self,
        source_session_id: int,
        cut: Callable[[ForkSource], ForkCut],
        *,
        title: str | None,
        session_type: SessionType,
        depth: int,
        deriving_call_id: str | None = None,
    ) -> SessionRecord:
        """两种分叉共用的编排：工作区闸门内一个写事务里定切点、建目标 Session、复制切点之前的历史。

        ``cut`` 在来源上取切点与初始配置，两种分叉只在这里不同。切点在写事务内算：点击到提交之间
        并入的新输入不会切错位置。``title`` 为 ``None`` 时按来源标题编号。提交后推送目录失效：
        Agent 分叉在父 Run 运行中建出，侧栏只能经推送得知。

        异常:
            SessionForkConflictError: 来源不能分叉，或工作区有其他结构性操作（``WORKSPACE_BUSY``）。
            ``cut`` 抛出的异常原样上浮。
        """
        source = self._session.get(source_session_id)
        try:
            with (
                workspace_operations.operation(source.workspace_id, timeout=10),
                begin_immediate(self._db_factory) as db,
            ):
                source = self._session.ensure_session(db, source_session_id)
                _require_forkable(source)
                fork_source = ForkSource(
                    session=source,
                    rows=self._session_context_crud.get(
                        source_session_id, include_in_context=False, db=db
                    ),
                    runs=self._turn.list_by_session_in_db(db, source.id),
                )
                fork_cut = cut(fork_source)
                target = self._session.create(
                    workspace_id=source.workspace_id,
                    title=title or self._session.next_fork_title(
                        source.workspace_id, source.title, db
                    ),
                    session_type=session_type,
                    extra=SessionExtra(fork=SessionFork(
                        source_session_id=source.id,
                        source_title=source.title,
                        tree_parent_session_id=source.id,
                        initial_launch=fork_cut.launch,
                    ), deriving_call_id=deriving_call_id),
                    # 工具列表是请求前缀的一部分，改了就接不上源的缓存；派生限制由运行期准入执行。
                    tool_schemas=deepcopy(source.tool_schemas),
                    db=db,
                    derivation_depth=depth,
                )
                count = fork_source.copy_before(fork_cut, target, self._session_context_crud, db)
        except TimeoutError as exc:
            raise SessionForkConflictError(
                "WORKSPACE_BUSY", "workspace has an active structural operation"
            ) from exc
        log.info(
            "session_fork_copied",
            extra={
                "msg": "copied context before the turn boundary",
                "data": {
                    "source_session_id": source.id,
                    "target_session_id": target.id,
                    "before_sequence": fork_cut.before_sequence,
                    "session_type": session_type,
                    "context_count": count,
                },
            },
        )
        service_depends.get_session_feed().publish_catalog(target.workspace_id, target.session_type)
        return target

    def list_sessions_for_workspace(self, workspace_id: int) -> list[SessionRecord]:
        """列出某工作区下的用户会话（排除委派子会话）。

        委派子会话（``session_type='delegated'``）不进侧边栏对话列表，故本方法仅返回
        ``user``、``user_fork`` 或 ``agent_fork`` 的会话，按更新时间倒序。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区下的用户会话记录列表（不含委派子会话）；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._session.list_by_workspace(workspace_id)

    async def delete_session_tree(self, session_id: int, *, with_forks: bool = False) -> frozenset[int]:
        """删除 Session 的用户用例：删除 = 停止 + 删除记录，停止在任何锁之外；返回实际删除的 Session id。

        经 :meth:`stop_then_delete` 先停下所有权子树（沿 ``parent_session_id`` 的委派子 Agent），再由
        :meth:`delete_session` 在一个事务里删除记录。
        fork 不在所有权子树里，默认不随源 Session 删除、只改挂（见 :meth:`delete_session_tree_in_session`）；
        ``with_forks`` 为真时把全部 fork 后代（含 fork 的 fork）当作额外的根一并停止和删除。fork 后代
        只在开头收集一次，删除的就是停下的这批：停止之后才 fork 出来的照常改挂，不会删掉没停下的 Session。

        删除提交后，仍在活动的派生 Session 的派生方（没有一起被删的）各得一条停止通知（见
        ``derived_report.report_deleted``）：派生方可能正挂起等它，没有通知就一直挂着。活动与否在
        每轮停止之前读，停下之后就都不活动了。

        异常:
            KeyError: Session 不存在。
            DeletionBusyError: workspace 正被其他结构性操作占用。
        """
        from app.core.tool.multi_agent import derived_report

        fork_ids = (
            await asyncio.to_thread(self.collect_fork_descendant_ids, session_id)
            if with_forks
            else frozenset()
        )
        active_derived: dict[int, SessionRecord] = {}

        def collect() -> set[int]:
            session_ids: set[int] = set()
            for root_id in (session_id, *fork_ids):
                session_ids |= self.collect_session_tree_ids(root_id)
            for session in self._active_derived(session_ids - active_derived.keys()):
                active_derived[session.id] = session
            return session_ids

        deleted = await self.stop_then_delete(collect, lambda: self.delete_session(session_id, fork_ids))
        await asyncio.to_thread(derived_report.report_deleted, active_derived.values(), deleted)
        return deleted

    def _active_derived(self, session_ids: set[int]) -> list[SessionRecord]:
        """其中有派生方、最新 Run 仍在活动（运行中或挂起）的 Session。"""
        found: list[SessionRecord] = []
        for session_id in session_ids:
            session = self.get_session(session_id)
            current = self.require_current_run(session_id)
            if session.deriver_id() is not None and current is not None and current.status in ACTIVE_RUN_STATUSES:
                found.append(session)
        return found

    async def stop_then_delete(
        self, collect: Callable[[], set[int]], delete: Callable[[], _T]
    ) -> _T:
        """删除一批 Session 的统一协议：在任何锁之外停下 ``collect()`` 给出的 Session，再执行 ``delete``。

        停止即墓碑（见 ``SessionRuntimeSpaceRegistry.stop``）；收集与停止反复到集合不再增长，覆盖父 Agent
        在停下前刚委派出的子 Session。``collect`` 与 ``delete`` 是同步函数，在工作线程里调用。``delete``
        没成功（闸门忙、写库失败）时为已停下的 Session 换上新 actor：Session 仍在，不能留着没有调度者。请求被取消时不回滚——工作线程里的删除可能照常提交，
        回滚会给已删除的 Session 拉起 actor。
        """
        stopped: set[int] = set()
        try:
            while True:
                session_ids = await asyncio.to_thread(collect)
                if session_ids <= stopped:
                    break
                await self._session_register.stop(session_ids - stopped)
                stopped |= session_ids
            return await asyncio.to_thread(delete)
        except Exception:
            self._session_register.restart_stopped(stopped)
            raise

    def existing_session_ids(self, session_ids: set[int]) -> set[int]:
        """``session_ids`` 中仍存在的 Session；id 自增不复用、删除不可逆，「已不存在」一经成立就不会变。"""
        return self._session.list_existing_ids(session_ids)

    def collect_fork_descendant_ids(self, session_id: int) -> frozenset[int]:
        """返回从 ``session_id`` fork 出的全部后代（fork 的 fork 也算），不含自身。

        fork 树关系记在 ``extra.fork.tree_parent_session_id``（不是 ``parent_session_id``），所以按工作区读出全部
        fork 的来源再从根向下展开。来源关系异常成环时，已访问的不重复展开。

        异常:
            KeyError: Session 不存在。
        """
        workspace_id = self._session.get(session_id).workspace_id
        children: dict[int, list[int]] = {}
        for fork_id, source_id in self._session.list_fork_sources(workspace_id).items():
            children.setdefault(source_id, []).append(fork_id)
        found: set[int] = set()
        pending = [session_id]
        while pending:
            for child_id in children.get(pending.pop(), []):
                if child_id != session_id and child_id not in found:
                    found.add(child_id)
                    pending.append(child_id)
        return frozenset(found)

    def delete_session(self, session_id: int, fork_ids: frozenset[int] = frozenset()) -> frozenset[int]:
        """原子删除会话树并级联清理其下全部子产物；调用方须已停下这棵树的 actor。

        删除前先校验会话存在（不存在则抛 ``KeyError``），再取得 workspace 闸门（与 fork、删除
        Run、删除 workspace 互斥）。不取各 Session 的闸门：它们只仲裁 actor 的调度轮次，actor 已在
        :meth:`delete_session_tree` 里停下。在单个 ``BEGIN IMMEDIATE`` 事务内按「子会话先于父会话」
        的后序清理每个会话自身及其产物，保证原子性（全删或全不删）。
        提交后卸载进程内 runtime space。Session 删除不负责
        删除图片附件，也不触发 workspace 附件孤儿 GC。删除是高风险操作，保留 start / complete
        审计日志。

        参数:
            session_id: 待删除的会话标识。
            fork_ids: 一并删除的 fork 后代（各自连同所有权子树），由 :meth:`delete_session_tree` 停下。

        返回:
            实际删除的全部 Session id（含各棵所有权子树）。

        异常:
            KeyError: 如果指定会话不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败（事务回滚）。

        副作用:
            从 ``session_contexts`` / ``runs`` / ``sessions`` 表删除该会话树
            相关数据；提交后推送目录失效、清理已删会话遗留的
            运行期缓存并卸载进程内 runtime space。
        """

        session = self._session.get(session_id)
        try:
            with workspace_operations.operation(session.workspace_id, timeout=10):
                deleted = self._delete_session_after_operations(session_id, fork_ids)
        except TimeoutError as exc:
            raise DeletionBusyError("session", session_id) from exc
        service_depends.get_session_feed().publish_catalog(session.workspace_id, session.session_type)
        log.info(
            "session_deleted",
            extra={
                "msg": "session deleted",
                "data": {
                    "session_id": session_id,
                    "fork_ids": sorted(fork_ids),
                    "deleted_sessions": len(deleted),
                },
            },
        )
        return deleted

    def cleanup_provisional_session(self, session_id: int, creation_command_id: str) -> None:
        """在 Assistant Transport 尚未接受首条命令时受控删除 provisional Session。

        只有会话创建标记完全匹配、会话树尚未产生任何 Run 时才允许删除。该用例复用普通
        Session 树删除核心，但明确不做图片附件删除或 workspace 附件孤儿 GC；清理失败由调用方
        保留原始 Transport 错误并记录诊断日志。
        """

        if not creation_command_id.strip():
            raise ProvisionalSessionCleanupConflictError("creationCommandId is required")
        session = self._session.get(session_id)
        if session.creation_command_id != creation_command_id:
            raise ProvisionalSessionCleanupConflictError("creationCommandId does not match session")
        try:
            with workspace_operations.operation(session.workspace_id, timeout=10):
                current = self._session.get(session_id)
                if current.creation_command_id != creation_command_id:
                    raise ProvisionalSessionCleanupConflictError(
                        "creationCommandId does not match session"
                    )
                tree_ids = self.collect_session_tree_ids(session_id)
                locked_ids = self._collect_session_ancestor_ids(session_id) | tree_ids
                with ExitStack() as stack:
                    for current_id in sorted(locked_ids):
                        stack.enter_context(
                            self._session_register.get_or_create(current_id).operation(timeout=10)
                        )
                    if any(self._turn.list_by_session(current_id) for current_id in tree_ids):
                        raise ProvisionalSessionCleanupConflictError(
                            "provisional session already has accepted business facts"
                        )
                    self._delete_session_after_operations(session_id)
        except TimeoutError as exc:
            raise DeletionBusyError("session", session_id) from exc
        service_depends.get_session_feed().publish_catalog(session.workspace_id, session.session_type)
        log.info(
            "provisional_session_cleaned",
            extra={
                "msg": "provisional session cleaned without attachment deletion",
                "data": {"session_id": session_id, "creation_command_id": creation_command_id},
            },
        )

    def _delete_session_after_operations(
        self, session_id: int, fork_ids: frozenset[int] = frozenset()
    ) -> frozenset[int]:
        """在已持有 workspace 闸门时提交会话树删除。"""

        log.info(
            "session_delete_start",
            extra={
                "msg": "session delete started",
                "data": {"session_id": session_id, "fork_ids": sorted(fork_ids)},
            },
        )
        with begin_immediate(self._db_factory) as db:
            deleted = self.delete_session_tree_in_db(session_id, db, fork_ids=fork_ids)
        self.finalize_deleted_session_spaces(deleted)
        return deleted

    def _collect_session_ancestor_ids(self, session_id: int) -> set[int]:
        """收集 session 自身以上的父 session 标识。

        参数:
            session_id: 起始 session 标识。

        返回:
            从起始 session 向上的全部父 session 标识，不含起始 session。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果读取父子关系失败。

        副作用:
            无；仅读取 session 关系。关系异常形成环时停止遍历，避免删除请求死循环。
        """

        ancestors: set[int] = set()
        current = self._session.get(session_id)
        while current.parent_session_id is not None:
            parent_id = current.parent_session_id
            if parent_id in ancestors or parent_id == session_id:
                break
            ancestors.add(parent_id)
            current = self._session.get(parent_id)
        return ancestors

    def collect_session_tree_ids(self, root_session_id: int) -> set[int]:
        """收集会话树（根会话及沿 ``parent_session_id`` 递归的全部子 Session）的 id 集合；
        删除前先停止这些 Session 的执行。"""

        with self._db_factory() as db:
            return set(self._collect_session_tree_ids_postorder(root_session_id, db))

    def _collect_session_tree_ids_postorder(self, root_session_id: int, db: DbSession) -> list[int]:
        """按「子会话先于父会话」的顺序返回会话树全部 id。

        ``sessions.parent_session_id`` 是即时检查的自引用外键，父行必须晚于全部子行删除。广度优先
        发现顺序里父会话总在子会话之前，反转即得所需顺序。
        """

        order = [root_session_id]
        index = 0
        while index < len(order):
            order.extend(self._session.list_child_ids(order[index], db))
            index += 1
        return order[::-1]

    def delete_session_tree_in_db(
        self, root_session_id: int, db: DbSession, *, fork_ids: frozenset[int] = frozenset()
    ) -> frozenset[int]:
        """按后序在同一事务删除所有权子树，返回提交后需卸载的 Session 集合。

        所有权子树沿 ``parent_session_id``（委派子 Agent）级联删除：它们是被删 Agent 执行的
        一部分，提升到上一级会把子 Agent 交给没有创建它的 Agent（``child_agent_*`` 按
        ``parent_session_id`` 授权、重启收尾按它回投结论）。fork 是独立对话，来源在 ``extra.fork``，
        不在子树里：``fork_ids`` 里的 fork 作为额外的根连同各自子树一起删，其余来源被删的 fork 改挂到
        被删根的来源（根不是 fork 时成为根），侧栏的 fork 树因此只少一层。fork 之间没有外键，
        各棵子树内部仍按后序删除即可。
        """

        session_ids_postorder = [
            current_id
            for tree_root_id in (root_session_id, *sorted(fork_ids))
            for current_id in self._collect_session_tree_ids_postorder(tree_root_id, db=db)
        ]
        deleted = frozenset(session_ids_postorder)
        root = self._session.ensure_session(db, root_session_id)
        self._session.reparent_forks(root.workspace_id, deleted, _fork_source(root, deleted), db)
        for current_id in session_ids_postorder:
            self._delete_single_session_in_db(current_id, db)
        return deleted

    def finalize_deleted_session_spaces(self, session_ids: frozenset[int]) -> None:
        """数据库提交后回收 Session 级资源（终端、浏览器标签、后台结果、看板）与运行时空间；回滚路径
        一概不碰。

        后台结果归 Session、比 Run 活得久，必须在这里撤销而不是指望删除前的停止：停止只在有执行者时
        撤销，空闲（Run 已完成、后台命令还在跑）或挂起（等后台结论）的 Session 没有执行者。撤销会
        终止后台命令，但不等它们退出（执行后端限时收尾）：占位行已随删除消失，不结算，这里还持着
        workspace 闸门。
        """
        from app.core.tool.handoff.handoff_registry import handoff_registry
        from app.tools.tool_handler.terminal.command_results import command_results

        service_depends.get_terminal_session_service().delete_session_terminals(session_ids)
        # 浏览器标签页归宿主所有：发后即忘的通知；宿主断线重连期间或后端崩溃时会丢，由链路连上时的
        # 对账补回收（``service/browser/tab_reclaim``）。
        service_depends.get_host_link().notify(
            "browser.dropSessions", {"session_ids": sorted(session_ids)}
        )
        for session_id in session_ids:
            command_results.drop_session(session_id)
            handoff_registry.drop_session(session_id)
        self._unload_deleted_session_spaces(set(session_ids))

    def _unload_deleted_session_spaces(self, session_ids: set[int]) -> None:
        """清除已删除会话的进程内 runtime space 与 context 引用。"""

        for current_id in session_ids:
            try:
                self._session_register.mark_deleted(current_id)
                self._state.release_deleted_session(current_id)
                space = self._session_register.get(current_id)
                if space is None:
                    continue
                space.unload_context_manager()
                self._session_register.unload(current_id, expected_space=space)
            except Exception:
                log.exception(
                    "session_runtime_cleanup_failed",
                    extra={
                        "msg": "session runtime cleanup failed after committed deletion",
                        "data": {"session_id": current_id},
                    },
                )

    def _delete_single_session_in_db(self, session_id: int, db: DbSession) -> None:
        """在调用方事务内删除 Session 与全部子产物；外键级联删除未消费 message_queue。"""

        self._session.ensure_session(db, session_id)  # 存在性守卫
        run_ids = self._turn.collect_run_ids_by_session_ids(db, [session_id])

        self._session_context_crud.delete_by_session_ids([session_id], db)
        self._turn.delete_by_ids(run_ids, db)
        self._session.delete_by_ids([session_id], db)


def _fork_source(session: SessionRecord, deleted: frozenset[int]) -> int | None:
    """被删根的 fork 来源（来源也在删除集里时视为无）：它的 fork 改挂到这里。"""
    if session.session_type not in FORK_SESSION_TYPES:
        return None
    source = cast(SessionFork, session.extra.fork).tree_parent_session_id
    return source if source not in deleted else None


def _require_forkable(source: SessionRecord) -> None:
    """只有侧栏会话能 fork：子 Agent 的工具面、系统提示词都是按只读 Session 定的，复制成
    侧栏会话会得到身份与能力对不上的混合体（前端只读面本来就不给 fork 入口）。"""
    if source.session_type not in USER_FACING_SESSION_TYPES:
        raise SessionForkConflictError(
            "FORK_SOURCE_UNSUPPORTED",
            "only sidebar conversations can be forked",
            retryable=False,
        )
