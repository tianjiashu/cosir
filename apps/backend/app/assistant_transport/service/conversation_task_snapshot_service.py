"""Task Transport snapshot 的唯一 owner。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any, ClassVar, cast

from sqlalchemy.orm import Session

from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.config.logging.logger import log
from app.storage.crud.conversation_task_snapshot_crud import ConversationTaskSnapshotCrud
from app.storage.store_engines import main_session_factory


@dataclass(frozen=True)
class SnapshotChange:
    """一次已提交的 snapshot 变化。"""

    task_id: int
    state: ConversationStateSnapshot
    mutations: tuple[ConversationStateMutation, ...]


@dataclass(frozen=True)
class _Subscriber:
    """绑定事件循环的进程内订阅者。"""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[SnapshotChange]


class ConversationTaskSnapshotService:
    """维护 Task snapshot 的唯一持久化、校验、缓存和发布边界。"""

    _lock: ClassVar[RLock] = RLock()
    _states: ClassVar[dict[int, ConversationStateSnapshot]] = {}
    _subscribers: ClassVar[dict[int, set[_Subscriber]]] = {}
    _cache_scope: ClassVar[str | None] = None

    def __init__(self) -> None:
        """绑定 snapshot CRUD。"""
        self._crud = ConversationTaskSnapshotCrud()

    def read(self, task_id: int) -> ConversationStateSnapshot:
        """从 SQLite 读取并校验最新 snapshot，缺失时创建空 snapshot。"""

        with self._lock:
            stored = self._crud.get(task_id)
            if stored is None:
                state = copy.deepcopy(empty_snapshot())
                self._crud.create(task_id, state)
            else:
                state = cast(ConversationStateSnapshot, stored)
            validate_snapshot(state)
            self._states[task_id] = copy.deepcopy(state)
            return copy.deepcopy(state)

    def apply(
        self,
        task_id: int,
        mutations: Sequence[ConversationStateMutation],
    ) -> SnapshotChange:
        """在最新 snapshot 上应用 mutation，提交成功后更新缓存并发布。"""

        with self._lock:
            with main_session_factory().begin() as session:
                current = self._crud.get_in_session(session, task_id)
                if current is None:
                    state: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
                else:
                    state = cast(ConversationStateSnapshot, current)
                for mutation in mutations:
                    _apply_mutation(state, mutation)
                validate_snapshot(state)
                self._crud.upsert_in_session(session, task_id, state)
            change = SnapshotChange(task_id, copy.deepcopy(state), tuple(mutations))
            self._states[task_id] = copy.deepcopy(state)
            if not mutations:
                return change
            self._publish(change)
            return change

    def apply_planned(
        self,
        task_id: int,
        planner: Callable[[ConversationStateSnapshot], Sequence[ConversationStateMutation]],
    ) -> SnapshotChange:
        """在 service 锁内基于最新 snapshot 规划并提交一次 mutation。"""

        with self._lock:
            with main_session_factory().begin() as session:
                current = self._crud.get_in_session(session, task_id)
                if current is None:
                    state: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
                else:
                    state = cast(ConversationStateSnapshot, current)
                mutations = tuple(planner(copy.deepcopy(state)))
                for mutation in mutations:
                    _apply_mutation(state, mutation)
                validate_snapshot(state)
                self._crud.upsert_in_session(session, task_id, state)
            change = SnapshotChange(task_id, copy.deepcopy(state), mutations)
            self._states[task_id] = copy.deepcopy(state)
            if not mutations:
                return change
            self._publish(change)
            return change

    def ensure_state_snapshot(
        self, task_id: int, session: Session | None = None
    ) -> ConversationStateSnapshot:
        """读取 snapshot；保留 session 参数供尚未迁移的创建流程读取。"""

        current = self._crud.get(task_id, session)
        if current is None:
            state: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
            self._crud.create(task_id, state, session)
        else:
            state = cast(ConversationStateSnapshot, current)
        validate_snapshot(state)
        return copy.deepcopy(state)

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

    def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Callable[[], None]]:
        """注册 Task 订阅者。"""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()
        subscriber = _Subscriber(loop, queue)
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(subscriber)

        def unsubscribe() -> None:
            """注销订阅者，重复调用安全。"""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

        return queue, unsubscribe


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
