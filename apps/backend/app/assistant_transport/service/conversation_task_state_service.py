"""进程内 Assistant Transport 状态与 canonical 冷读边界。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
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
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ConversationTaskStateService:
    """持有进程本地 Transport 状态，并按需从 canonical 记录懒重建。

    工作副本是刻意易失的，挂载在 task 的 ``TaskRuntimeSpace`` 中：projector 变更与 subscriber
    变化都不写任何数据库行。某个 task 的首次读取会加载一个 Task、其全部 Run 与全部 context 行，
    再把纯 wire 状态构造委托给 ``ConversationTaskStateRebuilder``。之后的读取复用该 task 级的
    进程内内存快照，直到显式 rebuild 或进程重置。

    参数:
        task_source: Task 的 CRUD 类数据源；默认取进程依赖。
        run_source: Run 的 CRUD 类数据源；默认取进程依赖。
        context_source: Context 的 CRUD 类数据源；默认取进程依赖。

    副作用:
        ``get_state`` 可能读取三张 canonical SQLite 表，并只缓存一份内存副本。
        ``apply_planned`` 与 ``publish_state`` 更新进程本地状态并通知 SSE subscriber。
        任何方法都不访问 checkpoint、工具、前端运行时状态或持久化的 Transport 快照。

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

    def __init__(
            self,
    ) -> None:
        """绑定冷读所需的 canonical 记录数据源。"""
        from app.service import depends
        self._task_source = depends.get_task_crud()
        self._run_source = depends.get_conversation_run_crud()
        self._context_source = depends.get_conversation_task_context_crud()

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
            mutations = tuple(event.plan(state))
            for mutation in mutations:
                _apply_mutation(state, mutation)
            # 仅追加的 token 帧不会改变快照形状或任何生命周期不变量。
            # ``_apply_mutation`` 中的 path/type 检查对这条热路径已足够；
            # 结构性变更仍付出完整校验代价。
            if _requires_full_snapshot_validation(mutations):
                validate_snapshot(state)
            change = TransportFrame(
                task_id=task_id,
                kind="mutation",
                mutations=mutations,
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
                try:
                    subscriber.loop.call_soon_threadsafe(subscriber.close)
                except RuntimeError:
                    pass
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
            subscribers = [subscriber for group in cls._subscribers.values() for subscriber in group]
            cls._subscribers.clear()
            cls._deleted_task_ids.clear()
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.close)
            except RuntimeError:
                pass
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
            from app.service.depends import get_delegation_service

            delegations = [
                record
                for run in runs
                for record in get_delegation_service().list_by_parent_turn(run.id)
            ]
            state = ConversationTaskStateRebuilder.rebuild(task, runs, context_rows, delegations)
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
