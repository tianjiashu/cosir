"""Task Transport snapshot 的唯一 owner。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any, ClassVar, Literal, cast

from sqlalchemy.orm import Session

from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.config.logging.logger import log
from app.config.settings import Settings
from app.storage.crud.conversation_task_snapshot_crud import ConversationTaskSnapshotCrud
from app.storage.store_engines import main_session_factory

MutationKind = Literal["set", "append-text"]
SnapshotPath = tuple[str | int, ...]


@dataclass(frozen=True)
class ConversationStateMutation:
    """一次官方 Assistant Transport state mutation。"""

    kind: MutationKind
    path: SnapshotPath
    value: object


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
    """维护 Task snapshot working copy，并在 commit 后发布变化。"""

    _lock: ClassVar[RLock] = RLock()
    _states: ClassVar[dict[int, ConversationStateSnapshot]] = {}
    _subscribers: ClassVar[dict[int, set[_Subscriber]]] = {}
    _cache_scope: ClassVar[str | None] = None

    def __init__(self) -> None:
        """绑定 snapshot CRUD。"""

        self._ensure_cache_scope()
        self._crud = ConversationTaskSnapshotCrud()

    @classmethod
    def _ensure_cache_scope(cls) -> None:
        """切换 SQLite 主库时清空进程内副本。"""

        scope = str(Settings.DATABASE_FILE)
        with cls._lock:
            if cls._cache_scope != scope:
                cls._states.clear()
                cls._subscribers.clear()
                cls._cache_scope = scope

    def load(self, task_id: int) -> ConversationStateSnapshot | None:
        """从 working copy 或 SQLite hydrate snapshot。"""

        with self._lock:
            cached = self._states.get(task_id)
            if cached is not None:
                return copy.deepcopy(cached)
        state = self._crud.get(task_id)
        if state is not None:
            self.hydrate(task_id, state)
        return copy.deepcopy(state) if state is not None else None

    def hydrate(self, task_id: int, state: ConversationStateSnapshot) -> None:
        """安装一份已校验的完整 snapshot working copy。"""

        validate_snapshot(state)
        with self._lock:
            self._states[task_id] = copy.deepcopy(state)

    def reconcile_run(
            self, task_id: int, run_id: int, status: str
    ) -> ConversationStateSnapshot | None:
        """用持久化 Run 的身份和状态修正 snapshot 的展示副本。

        Run 是执行事实的权威来源；该修正只更新进程内展示副本，不回写 snapshot，也不
        产生 Transport mutation。下一次 snapshot 独立写入或页面重新加载时会再次收敛。
        """

        state = self.load(task_id)
        if state is None:
            return state
        state["run"]["runId"] = run_id
        state["run"]["status"] = status
        if status in {"completed", "failed", "cancelled"}:
            index = find_assistant_message_index(state, run_id)
            if index is not None:
                state["messages"][index]["status"] = status
        self.hydrate(task_id, state)
        return state

    def ensure_state_snapshot(
            self, task_id: int, session: Session | None = None
    ) -> ConversationStateSnapshot:
        """在事务中读取或创建 snapshot。"""

        current: ConversationStateSnapshot | None = self._crud.get(task_id, session)
        if current is None:
            fallback: ConversationStateSnapshot = copy.deepcopy(empty_snapshot())
            self._crud.create(task_id, fallback, session)
            return fallback
        return current

    def stage(
            self,
            session: Session,
            task_id: int,
            mutations: Sequence[ConversationStateMutation],
            *,
            initial_state: ConversationStateSnapshot | None = None,
    ) -> ConversationStateSnapshot:
        """在调用方事务内应用 mutation 并写入候选 snapshot。"""

        pending = session.info.setdefault("conversation_snapshot_pending", {})
        if not isinstance(pending, dict):
            raise RuntimeError("invalid snapshot transaction context")
        item = pending.get(task_id)
        if item is None:
            with self._lock:
                cached = copy.deepcopy(self._states.get(task_id))
            base = cached or self._crud._get_in_session(session, task_id) or initial_state
            if base is None:
                raise KeyError(f"snapshot for task {task_id} is not initialized")
            item = {"state": copy.deepcopy(base), "mutations": []}
            pending[task_id] = item
        state = cast(ConversationStateSnapshot, item["state"])
        for mutation in mutations:
            _apply_mutation(state, mutation)
            item["mutations"].append(mutation)
        validate_snapshot(state)
        self._crud.upsert_in_session(session, task_id, state)
        return copy.deepcopy(state)

    def mutate(
            self,
            task_id: int,
            mutations: Sequence[ConversationStateMutation],
            *,
            initial_state: ConversationStateSnapshot | None = None,
    ) -> ConversationStateSnapshot:
        """在 snapshot 自己的独立写入中应用 mutation 并发布结果。

        snapshot 是 UI/Transport 展示事实，不与 Run 或 Agent context 组成强一致事务。
        调用成功后才替换进程内 working copy；失败时数据库写入和内存发布均不发生。
        """

        with main_session_factory().begin() as session:
            state = self.ensure_state_snapshot(
                session, task_id, initial_state or empty_snapshot()
            )
            next_state = self.stage(
                session, task_id, mutations, initial_state=state
            )
        self.hydrate(task_id, next_state)
        self._publish(
            SnapshotChange(task_id, copy.deepcopy(next_state), tuple(mutations))
        )
        return copy.deepcopy(next_state)

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

    def read_or_initialize(
            self, task_id: int, fallback: ConversationStateSnapshot
    ) -> ConversationStateSnapshot:
        """读取已有 snapshot；缺失时创建调用方提供的固定空快照。"""

        state = self.load(task_id)
        if state is not None:
            return state
        with main_session_factory().begin() as session:
            state = self.ensure_state_snapshot(session, task_id, fallback)
        self.hydrate(task_id, state)
        return copy.deepcopy(state)


def find_assistant_message_index(
        state: ConversationStateSnapshot, run_id: int
) -> int | None:
    """定位属于指定 run 的 assistant 消息在 snapshot messages 中的下标。

    参数:
        state: 待查询的 Task snapshot。
        run_id: 目标 Conversation Run 标识。

    返回:
        匹配 assistant 消息的下标；未找到（消息已被合并或清理）时返回 ``None``。

    异常:
        无。

    副作用:
        无。纯查询，不修改 ``state``。
    """

    for i, message in enumerate(state["messages"]):
        if message.get("runId") == run_id and message.get("role") == "assistant":
            return i
    return None


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
