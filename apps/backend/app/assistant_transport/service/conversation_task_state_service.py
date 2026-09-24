"""进程内 Assistant Transport 状态与 canonical 冷读边界。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from contextlib import suppress
from threading import RLock
from typing import Any, ClassVar, cast

from app.assistant_transport.event import ConversationEvent
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.service.terminal.session_status import (
    TerminalSessionStatusChange,
    TerminalSessionStatusSource,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_CHILD_RUN_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})
_TERMINAL_SESSION_ACTIVE_STATUSES = frozenset({"starting", "running"})
_TERMINAL_SESSION_STATUSES = frozenset(
    {"starting", "running", "exited", "interrupted", "failed", "closed"}
)


class ConversationTaskStateService:
    """持有进程本地 Transport 状态，并按需从 canonical 记录懒重建。

    工作副本是刻意易失的，挂载在 task 的 ``TaskRuntimeSpace`` 中：普通 projector 变更与 subscriber
    变化都不写数据库行；child Run 与 terminal session 状态投影只会更新已有工具行的 Transport
    metadata。某个 task 的
    首次读取会加载一个 Task、其全部 Run 与全部 context 行，
    再把纯 wire 状态构造委托给 ``ConversationTaskStateRebuilder``。之后的读取复用该 task 级的
    进程内内存快照，直到显式 rebuild 或进程重置。

    参数:
        task_source: Task 的 CRUD 类数据源；默认取进程依赖。
        run_source: Run 的 CRUD 类数据源；默认取进程依赖。
        context_source: Context 的 CRUD 类数据源；默认取进程依赖。

    副作用:
        ``get_state`` 可能读取三张 canonical SQLite 表，并只缓存一份内存副本。
            ``apply_planned`` 与 ``publish_state`` 更新进程本地状态并通知 SSE subscriber；child
            与 terminal 状态投影还会尽力持久化已有工具的 UI metadata。
        不访问 LangGraph checkpoint、工具 handler 或前端运行时状态；terminal session 只通过
        ``TerminalSessionStatusSource`` 读取进程内生命周期事实。

    并发:
        ``_lock``（类级 ``RLock``）是每个 ``TaskRuntimeSpace`` 快照工作副本的**唯一串行化点**：
        懒重建、projector 变更、发布、subscriber 注册与删除清理全部经过它。因此 space 自身不
        持有快照锁（见 ``app.task_runtime.task_runtime_space`` 模块 docstring）。直接访问 space
        仅在单线程下合法（状态生命周期测试即如此）；多线程调用方必须自行串行化，推荐做法是改走
        本 service 而非重新给 space 加锁。审查者不应把"缺少 per-space 锁"视为缺陷。
    """

    _lock: ClassVar[RLock] = RLock()
    _subscribers: ClassVar[dict[int, set[Subscriber]]] = {}
    _deleted_task_ids: ClassVar[set[int]] = set()
    _terminal_status_source: TerminalSessionStatusSource | None = None

    def __init__(
            self,
    ) -> None:
        """绑定冷读所需的 canonical 记录数据源。"""
        from app.service import depends
        self._task_source = depends.get_task_crud()
        self._run_source = depends.get_conversation_run_crud()
        self._context_source = depends.get_conversation_task_context_crud()
        self._terminal_status_source = depends.get_terminal_session_service()

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """返回已校验的 task 级工作副本，首次访问时懒重建。

        若 ``TaskRuntimeSpace`` 已持有快照则直接返回，不读 canonical 记录。数据库变更通过正常的
        projector/更新路径可见；普通读取不会重建或合并第二份副本。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_snapshot(lambda: self._rebuild(task_id))
            validate_snapshot(state)
            return state

    def rebuild_state(self, task_id: int) -> ConversationStateSnapshot:
        """丢弃进程内 working copy，并立即从 canonical 记录重建该 task 的快照。

        与 ``get_state`` 的区别是**强制**丢弃现有副本：调用方已确认快照缺少 canonical 确实
        存在的 run（二者分叉）时使用，避免继续在陈旧副本上投影。本方法是进程内唯一显式的
        快照自愈入口（``get_state`` 见到已物化副本不会重建，``unload_snapshot`` 此前只被
        删除清理使用）。

        参数:
            task_id: 目标任务标识。

        返回:
            重建后并通过 ``validate_snapshot`` 校验的 task 快照（深拷贝）。

        异常:
            KeyError: Task 在本进程内已被删除或不存在。
            ValueError: 重建结果不满足快照契约。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。

        副作用:
            先卸载该 task 的进程内快照副本，再读 canonical 三张表重建一份；terminal session
            冷重建可能原子修复遗留的活跃 display metadata；本方法不通知 subscriber（调用方需
            自行 ``publish_state`` 让客户端收敛）。

        并发:
            全程持 ``_lock``，与投影、订阅注册与删除清理互斥。
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            task_runtime_spaces.get_or_create(task_id).unload_snapshot()
            return self.get_state(task_id)

    def apply_planned(
            self,
            event: ConversationEvent
    ) -> TransportFrame:
        """将实时 projector 计划应用到进程本地状态并通知 subscriber。

        若不存在工作副本，canonical 冷读边界提供初始状态。planner 与变更在同一把锁下执行
        （与 subscriber 注册相同），因此首个 SSE 帧不会与排队的变更发生竞争。
        """
        task_id = event.task_id
        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            mutations = list(event.plan(state))
            for mutation in mutations:
                _apply_mutation(state, mutation)
            # A child can finish before the parent ToolMessage is persisted. Reconcile any
            # already-present child display payload after every event so that this race does
            # not leave the newly materialized parent card at ``running`` forever.
            mutations.extend(
                self._reconcile_child_delegation_statuses(
                    state,
                    persist=True,
                    parent_task_id=task_id,
                )
            )
            mutations.extend(
                self._reconcile_terminal_session_statuses(
                    state,
                    persist=True,
                    parent_task_id=task_id,
                )
            )
            # 仅追加的 token 帧不会改变快照形状或任何生命周期不变量。
            # ``_apply_mutation`` 中的 path/type 检查对这条热路径已足够；
            # 结构性变更仍付出完整校验代价。
            if _requires_full_snapshot_validation(mutations):
                validate_snapshot(state)
            change = TransportFrame(
                task_id=task_id,
                kind="mutation",
                mutations=tuple(mutations),
                source_run_id=getattr(event, "run_id", None),
                current_run_id=state["current_run_id"],
                current_run_status=_current_run_status(state),
            )
            if mutations:
                self._publish(change)
            return change

    def publish_state(
            self,
            task_id: int,
            state: ConversationStateSnapshot,
            mutations: tuple[ConversationStateMutation, ...] | None = None,
    ) -> TransportFrame:
        """发布一个其 canonical 数据库事务已经提交的状态。"""

        validate_snapshot(state)
        copied_state = copy.deepcopy(state)
        change = TransportFrame(
            task_id=task_id,
            kind="full",
            state=copied_state,
            mutations=mutations or (),
            current_run_id=copied_state["current_run_id"],
            current_run_status=_current_run_status(copied_state),
        )
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._publish(change)
        return change

    def subscribe_with_snapshot(
            self, task_id: int
    ) -> tuple[Subscriber, Callable[[], None], TransportFrame]:
        """在 ``_lock`` 下原子地挂载并准备首个 full 帧。"""

        loop = asyncio.get_running_loop()
        subscriber = Subscriber(loop)
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._subscribers.setdefault(task_id, set()).add(subscriber)
            try:
                initial = self.get_state(task_id)
                first_frame = TransportFrame(
                    task_id=task_id,
                    kind="full",
                    state=initial,
                    mutations=(),
                    current_run_id=initial["current_run_id"],
                    current_run_status=_current_run_status(initial),
                )
            except Exception:
                self._subscribers[task_id].discard(subscriber)
                if not self._subscribers[task_id]:
                    self._subscribers.pop(task_id, None)
                raise

        def unsubscribe() -> None:
            """移除本 subscriber；重复调用安全。"""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    subscriber.close()
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

        return subscriber, unsubscribe, first_frame

    def mark_task_deleted(self, task_id: int) -> None:
        """canonical Task 删除后，丢弃进程本地状态与 subscriber。"""

        with self._lock:
            self._deleted_task_ids.add(task_id)
            subscribers = self._subscribers.pop(task_id, set())
            for subscriber in subscribers:
                with suppress(RuntimeError):
                    subscriber.loop.call_soon_threadsafe(subscriber.close)
            space = task_runtime_spaces.get(task_id)
            if space is not None:
                space.unload_snapshot()

    @classmethod
    def clear_process_state(cls) -> None:
        """丢弃所有易失的进程本地状态。

        进程生命周期钩子，非持久化操作。清空 subscriber 队列与已删除 task 的墓碑，使同一解释器中
        后续的存储重新初始化不会从上一次后端生命周期继承陈旧的副本。
        """

        with cls._lock:
            subscribers = [
                subscriber
                for group in cls._subscribers.values()
                for subscriber in group
            ]
            cls._subscribers.clear()
            cls._deleted_task_ids.clear()
        for subscriber in subscribers:
            with suppress(RuntimeError):
                subscriber.loop.call_soon_threadsafe(subscriber.close)
        task_runtime_spaces.close()

    def is_task_deleted(self, task_id: int) -> bool:
        """返回本进程是否已对该 Task 完成删除收尾。"""

        with self._lock:
            return task_id in self._deleted_task_ids

    def _ensure_not_deleted(self, task_id: int) -> None:
        """进程本地删除清理后，拒绝读取或投影。"""

        if task_id in self._deleted_task_ids:
            raise KeyError(task_id)

    def _rebuild(self, task_id: int) -> ConversationStateSnapshot:
        """读取 canonical 记录，并以结构化日志调用纯状态重建器。"""

        log.info(
            "state_rebuild_started",
            extra={
                "msg": "rebuilding conversation state from canonical records",
                "data": {"task_id": task_id},
            },
        )
        try:
            task = self._task_source.get(task_id)
            runs = self._run_source.list_by_task(task_id)
            context_rows = self._context_source.get(task_id, include_in_context=False)
            state = ConversationTaskStateRebuilder.rebuild(
                task,
                runs,
                context_rows,
            )
            self._reconcile_child_delegation_statuses(state, persist=False)
            self._reconcile_terminal_session_statuses(
                state,
                persist=True,
                parent_task_id=task_id,
            )
        except Exception as exc:
            log.exception(
                "state_rebuild_failed",
                extra={
                    "msg": "conversation state rebuild failed",
                    "data": {
                        "task_id": task_id,
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "code", None),
                    },
                },
            )
            raise
        log.info(
            "state_rebuild_completed",
            extra={
                "msg": "conversation state rebuilt from canonical records",
                "data": {
                    "task_id": task_id,
                    "run_count": len(state["runs"]),
                    "message_count": sum(len(run["messages"]) for run in state["runs"]),
                },
            },
        )
        return state

    def refresh_parent_delegation(
        self,
        child_task_id: int,
        child_run_id: int,
        status: str,
    ) -> TransportFrame | None:
        """把 child Run 的 canonical 状态投影到已物化的父任务委派卡片。

        子 Run 的状态事件先更新子任务自身 snapshot，再调用此方法同步父任务中对应的
        动态 child display。该方法只处理已经存在且 locator 精确匹配的
        ``delegation-result`` 与 ``child-agent-result(operation=send)``；状态查询和等待
        结果是历史快照，不会被异步改写。父任务未物化时仍会先持久化 metadata，后续冷读
        可以恢复正确状态。
        """

        with self._lock:
            child_task = self._task_source.get(child_task_id)
            parent_task_id = child_task.parent_task_id
            if parent_task_id is None:
                return None
            child_run = next(
                (
                    run
                    for run in self._run_source.list_by_task(child_task_id)
                    if run.id == child_run_id
                ),
                None,
            )
            if child_run is None or child_run.status not in _CHILD_RUN_STATUSES:
                return None
            if child_run.status != status:
                log.debug(
                    "parent_delegation_stale_status_event",
                    extra={
                        "msg": "忽略与 canonical child Run 状态不一致的委派状态事件",
                        "data": {
                            "child_task_id": child_task_id,
                            "child_run_id": child_run_id,
                            "event_status": status,
                            "canonical_status": child_run.status,
                        },
                    },
                )
            status = child_run.status
            self._persist_child_display_status(
                parent_task_id,
                child_task_id,
                child_run_id,
                status,
            )
            parent_space = task_runtime_spaces.get(parent_task_id)
            if parent_space is None:
                return None
            state = parent_space.existing_snapshot()
            if state is None:
                return None

            mutations: list[ConversationStateMutation] = []
            for run_index, run in enumerate(state["runs"]):
                for message_index, message in enumerate(run["messages"]):
                    for part_index, part in enumerate(message["parts"]):
                        if not isinstance(part, dict) or part.get("type") != "tool-call":
                            continue
                        display = part.get("display_data")
                        if not _matches_dynamic_child_display(
                            display,
                            child_task_id,
                            child_run_id,
                        ):
                            continue
                        updated_display = dict(display)
                        updated_display["status"] = status
                        mutations.append(
                            ConversationStateMutation(
                                "set",
                                (
                                    "runs",
                                    run_index,
                                    "messages",
                                    message_index,
                                    "parts",
                                    part_index,
                                    "display_data",
                                ),
                                updated_display,
                            )
                        )
            if not mutations:
                return None
            for mutation in mutations:
                _apply_mutation(state, mutation)
            return self.publish_state(parent_task_id, state, tuple(mutations))

    def refresh_terminal_session(
        self,
        change: TerminalSessionStatusChange,
    ) -> TransportFrame | None:
        """把 backend 内 terminal session 的异步状态投影到已有工具卡片。

        ``TerminalSessionService`` 是 session 生命周期的唯一权威来源；本方法只定位已有的
        ``terminal-session`` display_data，不根据工具名或缺失 metadata 创建 UI part。父任务
        尚未物化时仍先原子持久化，之后的冷读可以恢复正确状态。
        """

        self._validate_terminal_status(change.status)
        with self._lock:
            self._persist_terminal_session_display(change)
            parent_space = task_runtime_spaces.get(change.task_id)
            if parent_space is None:
                return None
            state = parent_space.existing_snapshot()
            if state is None:
                return None

            mutations: list[ConversationStateMutation] = []
            for run_index, run in enumerate(state["runs"]):
                if run["runId"] != change.run_id:
                    continue
                for message_index, message in enumerate(run["messages"]):
                    for part_index, part in enumerate(message["parts"]):
                        if not isinstance(part, dict):
                            continue
                        display = part.get("display_data")
                        if not _matches_terminal_session_display(
                            display,
                            change.session_id,
                        ):
                            continue
                        updated_display = _updated_terminal_display(display, change)
                        if updated_display == display:
                            continue
                        mutations.append(
                            ConversationStateMutation(
                                "set",
                                (
                                    "runs",
                                    run_index,
                                    "messages",
                                    message_index,
                                    "parts",
                                    part_index,
                                    "display_data",
                                ),
                                updated_display,
                            )
                        )
            if not mutations:
                return None
            for mutation in mutations:
                _apply_mutation(state, mutation)
            return self.publish_state(change.task_id, state, tuple(mutations))

    def _reconcile_terminal_session_statuses(
        self,
        state: ConversationStateSnapshot,
        *,
        persist: bool,
        parent_task_id: int | None = None,
    ) -> list[ConversationStateMutation]:
        """按当前进程 terminal registry 收敛已有 terminal display 状态。"""

        if persist and parent_task_id is None:
            raise ValueError("parent_task_id is required when persisting terminal status")
        source = self._terminal_status_source
        if source is None:
            return []

        mutations: list[ConversationStateMutation] = []
        for run_index, run in enumerate(state["runs"]):
            run_id = run["runId"]
            for message_index, message in enumerate(run["messages"]):
                for part_index, part in enumerate(message["parts"]):
                    if not isinstance(part, dict):
                        continue
                    display = part.get("display_data")
                    if not _is_active_terminal_display(display):
                        continue
                    session_id = display.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        continue
                    change = source.get_status_change(
                        session_id,
                        task_id=parent_task_id or 0,
                        run_id=run_id,
                    )
                    if change is None:
                        change = TerminalSessionStatusChange(
                            task_id=parent_task_id or 0,
                            run_id=run_id,
                            session_id=session_id,
                            generation="",
                            status="closed",
                            end_reason="backend_restarted",
                            exit_code=None,
                        )
                    self._validate_terminal_status(change.status)
                    updated_display = _updated_terminal_display(display, change)
                    if updated_display == display:
                        continue
                    mutation = ConversationStateMutation(
                        "set",
                        (
                            "runs",
                            run_index,
                            "messages",
                            message_index,
                            "parts",
                            part_index,
                            "display_data",
                        ),
                        updated_display,
                    )
                    _apply_mutation(state, mutation)
                    mutations.append(mutation)
                    if persist and parent_task_id is not None:
                        self._persist_terminal_session_display(
                            change,
                            task_id=parent_task_id,
                        )
        return mutations

    def _persist_terminal_session_display(
        self,
        change: TerminalSessionStatusChange,
        *,
        task_id: int | None = None,
    ) -> None:
        """原子持久化已存在的 terminal-session display。"""

        self._validate_terminal_status(change.status)
        target_task_id = change.task_id if task_id is None else task_id
        try:
            rows = self._context_source.get(target_task_id, include_in_context=False)
            for row in rows:
                metadata = row.transport_metadata or {}
                display = metadata.get("display_data")
                if not _matches_terminal_session_display(display, change.session_id):
                    continue
                if row.run_id != change.run_id:
                    continue
                if _updated_terminal_display(display, change) == display:
                    continue
                self._context_source.update_terminal_session_display(
                    target_task_id,
                    row.sequence,
                    change.run_id,
                    change.session_id,
                    change.status,
                    change.end_reason,
                    change.exit_code,
                )
        except Exception:
            log.exception(
                "terminal_session_display_persist_failed",
                extra={
                    "msg": "Terminal session 状态已投影到内存，但 display metadata 持久化失败",
                    "data": {
                        "task_id": target_task_id,
                        "run_id": change.run_id,
                        "session_id": change.session_id,
                        "status": change.status,
                    },
                },
            )

    @staticmethod
    def _validate_terminal_status(status: str) -> None:
        """拒绝未声明的 terminal session 生命周期状态。"""

        if status not in _TERMINAL_SESSION_STATUSES:
            raise ValueError(f"invalid terminal session status: {status}")

    def _reconcile_child_delegation_statuses(
        self,
        state: ConversationStateSnapshot,
        *,
        persist: bool,
        parent_task_id: int | None = None,
    ) -> list[ConversationStateMutation]:
        """按已有 child display locator 修正其缓存的 canonical Run 状态。

        该修复只处理 snapshot 中已经存在的动态 child display，不会从工具名、Task 关系或
        缺失的 ``display_data`` 创建任何 UI part。冷重建因此仍保持 display_data 优先，
        但能收敛「子 Run 终态事件早于父 ToolMessage 持久化」留下的窗口。

        参数:
            state: 当前 task 的 Transport working copy。
            persist: 是否把发生变化的 metadata 同步回父任务 context；冷重建传 False，
                事件投影传 True。
            parent_task_id: 事件投影时用于持久化 metadata 的父任务标识；冷重建无需提供。

        返回:
            已应用到 state 的 display_data set mutations。

        副作用:
            ``persist=True`` 时更新父任务 context 的 Transport metadata；持久化失败只记
            日志，不阻断已经完成的内存 Transport 投影。
        """

        if persist and parent_task_id is None:
            raise ValueError("parent_task_id is required when persisting child status")

        locators: set[tuple[int, int]] = set()
        for run in state["runs"]:
            for message in run["messages"]:
                for part in message["parts"]:
                    if not isinstance(part, dict):
                        continue
                    display = part.get("display_data")
                    child_task_id = (
                        display.get("child_task_id")
                        if isinstance(display, dict)
                        else None
                    )
                    child_run_id = (
                        display.get("child_run_id")
                        if isinstance(display, dict)
                        else None
                    )
                    if not _is_dynamic_child_display(display):
                        continue
                    if not _is_positive_int(child_task_id) or not _is_positive_int(child_run_id):
                        continue
                    locators.add((child_task_id, child_run_id))

        statuses_by_locator: dict[tuple[int, int], str] = {}
        for child_task_id in {task_id for task_id, _ in locators}:
            child_runs = self._run_source.list_by_task(child_task_id)
            for child_run in child_runs:
                locator = (child_task_id, child_run.id)
                if locator not in locators:
                    continue
                if (
                    child_run.task_id != child_task_id
                    or child_run.status not in _CHILD_RUN_STATUSES
                ):
                    continue
                statuses_by_locator[locator] = child_run.status

        mutations: list[ConversationStateMutation] = []
        for run_index, run in enumerate(state["runs"]):
            for message_index, message in enumerate(run["messages"]):
                for part_index, part in enumerate(message["parts"]):
                    if not isinstance(part, dict):
                        continue
                    display = part.get("display_data")
                    if not isinstance(display, dict) or not _is_dynamic_child_display(display):
                        continue
                    child_task_id = display.get("child_task_id")
                    child_run_id = display.get("child_run_id")
                    if not _is_positive_int(child_task_id) or not _is_positive_int(child_run_id):
                        continue
                    status = statuses_by_locator.get((child_task_id, child_run_id))
                    if status is None or display.get("status") == status:
                        continue
                    updated_display = dict(display)
                    updated_display["status"] = status
                    mutation = ConversationStateMutation(
                        "set",
                        (
                            "runs",
                            run_index,
                            "messages",
                            message_index,
                            "parts",
                            part_index,
                            "display_data",
                        ),
                        updated_display,
                    )
                    _apply_mutation(state, mutation)
                    mutations.append(mutation)
                    if persist and parent_task_id is not None:
                        self._persist_child_display_status(
                            parent_task_id,
                            child_task_id,
                            child_run_id,
                            status,
                        )
        return mutations

    def _persist_child_display_status(
        self,
        parent_task_id: int,
        child_task_id: int,
        child_run_id: int,
        status: str,
    ) -> None:
        """持久化父任务中精确 locator 命中的动态 child display 状态。"""

        if status not in _CHILD_RUN_STATUSES:
            return
        try:
            rows = self._context_source.get(parent_task_id, include_in_context=False)
            for row in rows:
                metadata = row.transport_metadata or {}
                display = metadata.get("display_data")
                if not isinstance(display, dict) or not _matches_dynamic_child_display(
                    display,
                    child_task_id,
                    child_run_id,
                ):
                    continue
                self._context_source.update_child_display_status(
                    parent_task_id,
                    row.sequence,
                    child_task_id,
                    child_run_id,
                    status,
                )
        except Exception:
            log.exception(
                "parent_delegation_display_persist_failed",
                extra={
                    "msg": "子 Run 状态已投影到内存，但父委派 display metadata 持久化失败",
                    "data": {
                        "parent_task_id": parent_task_id,
                        "child_task_id": child_task_id,
                        "child_run_id": child_run_id,
                        "status": status,
                    },
                },
            )

    def _publish(self, change: TransportFrame) -> None:
        """存在 full 状态时安装该状态，并将帧入队给各 subscriber。"""

        if change.state is not None:
            task_runtime_spaces.get_or_create(change.task_id).replace_snapshot(change.state)
        subscribers = tuple(self._subscribers.get(change.task_id, set()))
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, change)
            except RuntimeError:
                log.warning(
                    "conversation_snapshot_subscriber_closed",
                    extra={
                        "msg": "snapshot subscriber loop closed",
                        "data": {"task_id": change.task_id},
                    },
                )


def _is_active_terminal_display(value: object) -> bool:
    """判断 display_data 是否是需要实时收敛的活 terminal session。"""

    return (
        isinstance(value, dict)
        and value.get("kind") == "terminal-session"
        and isinstance(value.get("session_id"), str)
        and bool(value.get("session_id"))
        and value.get("status") in _TERMINAL_SESSION_ACTIVE_STATUSES
    )


def _matches_terminal_session_display(value: object, session_id: str) -> bool:
    """按 kind 与 session_id 精确定位 terminal display。"""

    return (
        isinstance(value, dict)
        and value.get("kind") == "terminal-session"
        and value.get("session_id") == session_id
    )


def _updated_terminal_display(
    value: object,
    change: TerminalSessionStatusChange,
) -> dict[str, object]:
    """复制 display_data 并仅更新 terminal 生命周期字段。"""

    if not isinstance(value, dict):
        raise TypeError("terminal display_data must be an object")
    current_status = value.get("status")
    if current_status in {"exited", "interrupted", "failed", "closed"}:
        return dict(value)
    if current_status == "running" and change.status == "starting":
        return dict(value)
    updated = dict(value)
    updated["status"] = change.status
    if change.end_reason is not None:
        updated["end_reason"] = change.end_reason
    if change.exit_code is not None:
        updated["exit_code"] = change.exit_code
    return updated


def _is_positive_int(value: object) -> bool:
    """Return whether a child locator is a real positive integer."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_dynamic_child_display(value: object) -> bool:
    """Return whether a display payload represents a live child Run reference."""

    if not isinstance(value, dict):
        return False
    if value.get("kind") == "delegation-result":
        return True
    return value.get("kind") == "child-agent-result" and value.get("operation") == "send"


def _matches_dynamic_child_display(
    value: object,
    child_task_id: int,
    child_run_id: int,
) -> bool:
    """Match a live child display by both task and Run locator."""

    if not _is_dynamic_child_display(value) or not isinstance(value, dict):
        return False
    return (
        value.get("child_task_id") == child_task_id
        and value.get("child_run_id") == child_run_id
    )


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """将 Transport 的 set/append-text 变更应用到内存状态。"""

    mutable_state = cast(dict[str, Any], state)
    if not mutation.path:
        if mutation.kind != "set" or not isinstance(mutation.value, dict):
            raise ValueError("root mutation must be set with an object")
        mutable_state.clear()
        mutable_state.update(copy.deepcopy(cast(dict[str, Any], mutation.value)))
        return
    parent: Any = mutable_state
    for key in mutation.path[:-1]:
        parent = parent[key]
    key = mutation.path[-1]
    if mutation.kind == "set":
        value = copy.deepcopy(mutation.value)
        if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
        return
    if mutation.kind != "append-text" or not isinstance(mutation.value, str):
        raise ValueError("append-text mutation must carry a string")
    current = parent[key]
    if not isinstance(current, str):
        raise TypeError("append-text target must be a string")
    parent[key] = current + mutation.value


def _current_run_status(state: ConversationStateSnapshot) -> str | None:
    """在不做完整投影的前提下，返回当前 run 状态用于连接诊断。"""

    current_id = state["current_run_id"]
    if current_id is None:
        return None
    for run in state["runs"]:
        if run["runId"] == current_id:
            return run["status"]
    return None


def _requires_full_snapshot_validation(
        mutations: tuple[ConversationStateMutation, ...],
) -> bool:
    """返回一批变更是否会改变快照结构或生命周期形状。"""

    return any(
        mutation.kind != "append-text"
        or not mutation.path
        or not isinstance(mutation.value, str)
        or not mutation.value
        for mutation in mutations
    )


__all__ = ["ConversationTaskStateService"]
