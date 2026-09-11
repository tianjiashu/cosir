"""Task Transport snapshot 的唯一 owner。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Any, ClassVar, cast

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunStatusChangedEvent, ToolCallsSettledEvent
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import SnapshotChange
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.models import ConversationRunStatus
from app.models.errors.task_fork_errors import SnapshotNotReadyError
from app.storage.crud.conversation_task_snapshot_crud import ConversationTaskSnapshotCrud
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ConversationTaskSnapshotService:
    """维护 Task snapshot 的唯一持久化、校验、缓存和发布边界。"""

    _lock: ClassVar[RLock] = RLock()
    _states: ClassVar[dict[int, ConversationStateSnapshot]] = {}
    _subscribers: ClassVar[dict[int, set[Subscriber]]] = {}
    _deleted_task_ids: ClassVar[set[int]] = set()
    _cache_scope: ClassVar[str | None] = None

    def __init__(self) -> None:
        """绑定 snapshot CRUD。"""
        self._crud = ConversationTaskSnapshotCrud()

    def apply_planned(
        self,
        task_id: int,
        planner: Callable[[ConversationStateSnapshot], Sequence[ConversationStateMutation]],
        session: Session | None = None,
    ) -> SnapshotChange:
        """在 service 锁内基于最新 snapshot 规划并应用一次 mutation。

        传入 ``session`` 时复用调用方事务；否则创建并提交独立事务。
        """

        with self._lock:
            if task_id in self._deleted_task_ids:
                raise KeyError(task_id)
            if session is None:
                with main_session_factory().begin() as owned_session:
                    state, mutations = self._apply_planned_in_session(
                        owned_session, task_id, planner
                    )
                should_publish = True
            else:
                state, mutations = self._apply_planned_in_session(session, task_id, planner)
                should_publish = False
            change = SnapshotChange(task_id, copy.deepcopy(state), mutations)
            self._states[task_id] = copy.deepcopy(state)
            if not mutations or not should_publish:
                return change
            self._publish(change)
            return change

    def _apply_planned_in_session(
        self,
        session: Session,
        task_id: int,
        planner: Callable[[ConversationStateSnapshot], Sequence[ConversationStateMutation]],
    ) -> tuple[ConversationStateSnapshot, tuple[ConversationStateMutation, ...]]:
        """在指定 Session 中规划并写入 snapshot，不负责提交或发布通知。"""

        current = self._crud.get_in_session(session, task_id)
        if current is None:
            state: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
        else:
            state = copy.deepcopy(current)
        mutations = tuple(planner(copy.deepcopy(state)))
        for mutation in mutations:
            _apply_mutation(state, mutation)
        validate_snapshot(state)
        self._crud.upsert_in_session(session, task_id, state)
        return state, mutations

    def ensure_state_snapshot(
        self, task_id: int, session: Session | None = None
    ) -> ConversationStateSnapshot:
        """读取 snapshot；保留 session 参数供尚未迁移的创建流程读取。"""

        with self._lock:
            if task_id in self._deleted_task_ids:
                raise KeyError(task_id)
        current = self._crud.get(task_id, session)
        if current is None:
            state: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
            self._crud.create(task_id, state, session)
        else:
            state = copy.deepcopy(current)
        validate_snapshot(state)
        return copy.deepcopy(state)

    def clone_for_fork(
        self,
        source_task_id: int,
        target_task_id: int,
        run_id_map: dict[int, int],
        session: Session,
    ) -> ConversationStateSnapshot:
        """在外部事务中复制并重置一个 Task 的历史 snapshot。

        该方法只写入调用方事务，不提前更新进程缓存或发布目标 Task 事件；调用方提交
        成功后再调用 :meth:`cache_committed_snapshot`。
        """

        with self._lock:
            source = self._crud.get_in_session(session, source_task_id)
            if source is None:
                raise SnapshotNotReadyError(f"snapshot for task {source_task_id} is not ready")
            try:
                state = copy.deepcopy(source)
                validate_snapshot(state)
            except (TypeError, ValueError, KeyError) as exc:
                raise SnapshotNotReadyError(
                    f"snapshot for task {source_task_id} is not ready"
                ) from exc

            runs = []
            for run in state["runs"]:
                source_run_id = run["runId"]
                if source_run_id not in run_id_map:
                    continue
                cloned_run = copy.deepcopy(run)
                cloned_run["runId"] = run_id_map[source_run_id]
                runs.append(cloned_run)
            state["runs"] = runs
            state["current_run_id"] = None
            state["error"] = None
            state["approvals"] = {}
            state["context_usage_ratio"] = None
            state["context_usage_used"] = None
            state["context_window_total"] = None
            validate_snapshot(state)
            self._crud.upsert_in_session(session, target_task_id, state)
            return copy.deepcopy(state)

    def cache_committed_snapshot(self, task_id: int, state: ConversationStateSnapshot) -> None:
        """在 fork 事务提交后登记目标 snapshot 的进程缓存。"""

        with self._lock:
            if task_id in self._deleted_task_ids:
                return
            self._states[task_id] = copy.deepcopy(state)

    def mark_task_deleted(self, task_id: int) -> None:
        """标记 Task 已删除并清理其 snapshot 缓存与订阅者引用。

        参数:
            task_id: 已经完成数据库级删除的任务标识。

        返回:
            无。

        异常:
            无。

        副作用:
            清除进程内 snapshot working copy 和订阅关系；后续 snapshot 读取或投影
            不会为该 task 重新创建 snapshot。
        """

        with self._lock:
            self._deleted_task_ids.add(task_id)
            self._states.pop(task_id, None)
            self._subscribers.pop(task_id, None)

    def is_task_deleted(self, task_id: int) -> bool:
        """返回 Task 是否已被当前 backend 进程标记为删除。"""

        with self._lock:
            return task_id in self._deleted_task_ids

    def reset_run_for_edit(
        self,
        task_id: int,
        run_id: int,
        input_text: str,
        session: Session | None = None,
    ) -> ConversationStateSnapshot:
        """原地重置当前 run 的 Transport 消息，保留消息身份并替换输入。

        传入外部 session 时只写入该事务，不更新进程缓存或发布订阅通知；调用方提交
        成功后必须调用 ``publish_committed_snapshot``。
        """

        with self._lock:
            if session is None:
                with main_session_factory().begin() as owned_session:
                    next_state = self._reset_run_for_edit_in_session(
                        owned_session, task_id, run_id, input_text
                    )
            else:
                next_state = self._reset_run_for_edit_in_session(
                    session, task_id, run_id, input_text
                )
            if session is not None:
                return copy.deepcopy(next_state)
            change = SnapshotChange(
                task_id,
                copy.deepcopy(next_state),
                (ConversationStateMutation("set", (), next_state),),
            )
            self._states[task_id] = copy.deepcopy(next_state)
            self._publish(change)
            return copy.deepcopy(next_state)

    def _reset_run_for_edit_in_session(
        self,
        session: Session,
        task_id: int,
        run_id: int,
        input_text: str,
    ) -> ConversationStateSnapshot:
        """在外部事务中重置 run snapshot，不更新缓存或发布通知。"""

        current = self._crud.get_in_session(session, task_id)
        if current is None:
            raise KeyError(task_id)
        state = copy.deepcopy(current)
        next_state: ConversationStateSnapshot = copy.deepcopy(state)
        target_run = next(
            (run for run in next_state["runs"] if run["runId"] == run_id),
            None,
        )
        if target_run is None:
            raise KeyError(f"run {run_id} has no user message in task snapshot")
        user_found = False
        for message in target_run["messages"]:
            if message["role"] == "user":
                user_found = True
                message["parts"] = [{"type": "text", "text": input_text, "status": "completed"}]
            else:
                message["parts"] = []
        if not user_found:
            raise KeyError(f"run {run_id} has no user message in task snapshot")
        target_run["status"] = "pending"
        target_run["endReason"] = None
        target_run["usage"] = None
        next_state["current_run_id"] = run_id
        next_state["context_usage_ratio"] = None
        next_state["context_usage_used"] = None
        next_state["context_window_total"] = None
        next_state["error"] = None
        validate_snapshot(next_state)
        self._crud.upsert_in_session(session, task_id, next_state)
        return next_state

    def publish_committed_snapshot(
        self,
        task_id: int,
        state: ConversationStateSnapshot,
        mutations: tuple[ConversationStateMutation, ...] | None = None,
    ) -> SnapshotChange:
        """发布已经由外部事务成功提交的 snapshot。"""

        validate_snapshot(state)
        change = SnapshotChange(
            task_id,
            copy.deepcopy(state),
            mutations or (ConversationStateMutation("set", (), copy.deepcopy(state)),),
        )
        with self._lock:
            self._publish(change)
        return change

    def _publish(self, change: SnapshotChange) -> None:
        """更新 snapshot working copy 并通知订阅者。"""

        with self._lock:
            self._states[change.task_id] = copy.deepcopy(change.state)
            subscribers = tuple(self._subscribers.get(change.task_id, set()))
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, change)
            except RuntimeError:
                log.warning(
                    "conversation_snapshot_subscriber_closed",
                    extra={
                        "msg": "snapshot subscriber loop closed",
                        "data": {"task_id": change.task_id},
                    },
                )

    def subscribe_with_snapshot(
        self, task_id: int
    ) -> tuple[asyncio.Queue[SnapshotChange], Callable[[], None], ConversationStateSnapshot]:
        """原子注册订阅者并读取首帧 snapshot。

        注册和首帧读取必须共享 service 锁。否则 mutation 可能在 ``subscribe`` 与
        ``ensure_state_snapshot`` 之间提交，导致首帧已经包含 mutation 后的完整状态，
        但队列又留下同一 mutation，Assistant Transport 会把增量重复应用。
        """

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()
        subscriber = Subscriber(loop, queue)
        with self._lock:
            if task_id in self._deleted_task_ids:
                raise KeyError(task_id)
            self._subscribers.setdefault(task_id, set()).add(subscriber)
            initial = self.ensure_state_snapshot(task_id)

        def unsubscribe() -> None:
            """注销订阅者，重复调用安全。"""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

        return queue, unsubscribe, initial

    async def read(self, task_id: int) -> ConversationStateSnapshot:
        """读取 canonical snapshot，并在读取边界收敛终态一致性。

        后端启动时由 ``ConversationRunService.recover_orphaned_runs`` 将崩溃前遗留的
        ``pending``/``running`` Run 原子收敛为 ``cancelled``，且不直接修改 snapshot。
        因此本方法负责读取时发现「Run 已是终态、snapshot 仍是旧状态」的窗口，并经
        ``ConversationEventProjector`` 补齐 snapshot；不会因为 transport 断开、任务切换或
        当前进程暂时没有 executor entry 就取消 Run，也不会自动 resume。
        """
        from app.service.depends import (
            get_conversation_event_projector,
            get_conversation_run_executor,
            get_conversation_run_service,
        )

        run_executor = get_conversation_run_executor()
        run_service = get_conversation_run_service()
        projector = get_conversation_event_projector()
        task_space = task_runtime_spaces.get_or_create(task_id)

        async def read_locked() -> ConversationStateSnapshot:
            """在已取得 Task 闸门后执行 snapshot recovery 读取。"""

            state: ConversationStateSnapshot = self.ensure_state_snapshot(task_id)
            runs = run_service.list_runs_for_task(task_id)
            for run in runs:
                snapshot_run = next(
                    (item for item in state["runs"] if item["runId"] == run.id),
                    None,
                )
                if (
                    run.status
                    in {
                        ConversationRunStatus.COMPLETED.value,
                        ConversationRunStatus.FAILED.value,
                        ConversationRunStatus.CANCELLED.value,
                    }
                    and snapshot_run is not None
                    and snapshot_run["status"] != run.status
                ):
                    # A process can die after the run row commits but before its final
                    # projector event. Reconcile the snapshot at the read boundary so
                    # resume cannot mistake a DB-terminal run for an active stream.
                    projector.process(
                        RunStatusChangedEvent(
                            task_id=task_id,
                            run_id=run.id,
                            status=ConversationRunStatus(run.status),
                            end_reason=run.end_reason,
                        )
                    )
                    state = self.ensure_state_snapshot(task_id)
                if (
                    run.status
                    in {
                        ConversationRunStatus.COMPLETED.value,
                        ConversationRunStatus.FAILED.value,
                        ConversationRunStatus.CANCELLED.value,
                    }
                    and snapshot_run is not None
                    and _has_open_tool_calls(snapshot_run)
                ):
                    # A historical workflow can finish the Run after dropping its tool
                    # observation result. Do not leave the Transport part spinning forever:
                    # close the orphaned UI facts at the read boundary without replaying a
                    # tool or changing the authoritative Run row.
                    projector.process(
                        ToolCallsSettledEvent(
                            task_id=task_id,
                            run_id=run.id,
                            status=(
                                "cancelled"
                                if run.status == ConversationRunStatus.CANCELLED.value
                                else "failed"
                            ),
                            reason="snapshot_reconciled_open_tool_calls",
                        )
                    )
                    state = self.ensure_state_snapshot(task_id)
                    snapshot_run = next(
                        (item for item in state["runs"] if item["runId"] == run.id),
                        None,
                    )
                if run.status not in {
                    ConversationRunStatus.PENDING.value,
                    ConversationRunStatus.RUNNING.value,
                }:
                    continue
                if not run_executor.is_locally_running(run.id):
                    log.info(
                        "conversation_run_recovery_required",
                        extra={
                            "msg": "发现当前进程未登记但仍可恢复的 Conversation Run",
                            "data": {
                                "task_id": task_id,
                                "run_id": run.id,
                                "run_status": run.status,
                            },
                        },
                    )
            return self.ensure_state_snapshot(task_id)

        async_operation = getattr(task_space, "async_operation", None)
        if callable(async_operation):
            async with async_operation(wait_seconds=10):
                return await read_locked()

        # 轻量测试替身可能只实现旧的同步 lock 接口；生产 TaskRuntimeSpace 始终
        # 走上面的共享异步适配器。兼容路径也放入线程池，避免阻塞 event loop。
        acquired = await asyncio.to_thread(
            task_space.lock.acquire,
            blocking=True,
            timeout=10,
        )
        if not acquired:
            raise TimeoutError(f"task {task_id} recovery lock is busy")
        try:
            return await read_locked()
        finally:
            task_space.lock.release()


def _has_open_tool_calls(snapshot_run: dict[str, Any]) -> bool:
    """判断终态 Run 的 snapshot 是否仍包含未收口工具 part。"""

    return any(
        isinstance(part, dict)
        and part.get("type") == "tool-call"
        and part.get("status") in {"pending", "running"}
        for message in snapshot_run.get("messages", [])
        if isinstance(message, dict)
        for part in message.get("parts", [])
    )


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """在 JSON state 上应用官方 set/append-text 操作。"""

    mutable_state = cast(dict[str, Any], state)

    if not mutation.path:
        if mutation.kind != "set" or not isinstance(mutation.value, dict):
            raise ValueError("root mutation must be set with an object")
        replacement = copy.deepcopy(mutation.value)
        mutable_state.clear()
        mutable_state.update(cast(dict[str, Any], replacement))
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
