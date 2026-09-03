"""Task ConversationState 快照 owner 与进程内 notifier。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import ClassVar, Literal, cast

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.config.settings import Settings
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.storage.crud.conversation_task_snapshot_crud import ConversationTaskSnapshotCrud

MutationKind = Literal["set", "append-text"]
SnapshotPath = tuple[str | int, ...]


@dataclass(frozen=True)
class ConversationStateMutation:
    """一次不含 assistant-ui 依赖的 Transport state mutation。"""

    kind: MutationKind
    path: SnapshotPath
    value: object


@dataclass(frozen=True)
class SnapshotChange:
    """已提交的快照及其对应局部 mutation。"""

    task_id: int
    state: ConversationStateSnapshot
    mutations: tuple[ConversationStateMutation, ...]


@dataclass(frozen=True)
class _Subscriber:
    """绑定到某个 asyncio loop 的进程内订阅者。"""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[SnapshotChange]


class ConversationTaskSnapshotService:
    """维护 Task 快照的唯一进程内工作副本并负责提交后通知。

    SQLite 快照是重启后的持久化来源，``_states`` 是当前进程内的实时副本；本服务不
    读取 ``conversation_messages`` 等旧表，也不生成第二份 Transport 事实。
    """

    _lock: ClassVar[RLock] = RLock()
    _states: ClassVar[dict[int, ConversationStateSnapshot]] = {}
    _subscribers: ClassVar[dict[int, set[_Subscriber]]] = {}
    _cache_scope: ClassVar[str | None] = None

    def __init__(self) -> None:
        """绑定快照单表 CRUD。"""

        self._ensure_cache_scope()
        self._crud = ConversationTaskSnapshotCrud()

    @classmethod
    def _ensure_cache_scope(cls) -> None:
        """确保进程内缓存只属于当前 SQLite 主库。"""

        scope = str(Settings.DATABASE_FILE)
        with cls._lock:
            if cls._cache_scope == scope:
                return
            cls._states.clear()
            cls._subscribers.clear()
            cls._cache_scope = scope

    def load(self, task_id: int) -> ConversationStateSnapshot | None:
        """读取并缓存 Task 快照；进程内已有副本时不访问数据库。"""

        with self._lock:
            cached = self._states.get(task_id)
            if cached is not None:
                return copy.deepcopy(cached)
        state = self._crud.get(task_id)
        if state is None:
            return None
        self.hydrate(task_id, state)
        return copy.deepcopy(state)

    def hydrate(self, task_id: int, state: ConversationStateSnapshot) -> None:
        """用持久化或新建后的完整快照填充进程内副本。"""

        with self._lock:
            self._states[task_id] = copy.deepcopy(state)

    def upsert_in_session(
        self,
        session: Session,
        task_id: int,
        state: ConversationStateSnapshot,
    ) -> None:
        """在调用方事务中持久化一个完整快照。"""

        self._crud.upsert_in_session(session, task_id, state)

    def ensure_in_session(
        self,
        session: Session,
        task_id: int,
        fallback: ConversationStateSnapshot,
    ) -> ConversationStateSnapshot:
        """在调用方事务内确保 Task 有快照，并返回事务内的当前 state。"""

        current = self._crud.get_in_session(session, task_id)
        if current is None:
            self._crud.upsert_in_session(session, task_id, fallback)
            return copy.deepcopy(fallback)
        return current

    def stage(
        self,
        session: Session,
        task_id: int,
        mutations: Sequence[ConversationStateMutation],
        *,
        initial_state: ConversationStateSnapshot | None = None,
    ) -> ConversationStateSnapshot:
        """在当前数据库事务内应用 mutation 并写入 state_json。

        只有外层事务成功提交后，``SnapshotChange`` 才会被发布并替换进程内副本；事务
        回滚不会污染内存状态。一次事务内的多次调用会在同一工作副本上顺序应用。
        """

        pending = session.info.setdefault("conversation_snapshot_pending", {})
        if not isinstance(pending, dict):
            raise RuntimeError("invalid conversation snapshot transaction context")
        working = pending.get(task_id)
        if working is None:
            base: ConversationStateSnapshot | None
            with self._lock:
                base = copy.deepcopy(self._states.get(task_id))
            if base is None:
                base = self._crud.get_in_session(session, task_id)
            if base is None:
                base = copy.deepcopy(initial_state)
            if base is None:
                raise KeyError(f"conversation snapshot not initialized for task {task_id}")
            working = {"state": base, "mutations": []}
            pending[task_id] = working
        state = cast(ConversationStateSnapshot, working["state"])
        for mutation in mutations:
            _apply_mutation(state, mutation)
            working["mutations"].append(mutation)
        self._crud.upsert_in_session(session, task_id, state)
        return copy.deepcopy(state)

    def publish_transaction(self, session: Session) -> None:
        """在外层事务 commit 成功后发布本事务的所有快照变更。"""

        pending = session.info.pop("conversation_snapshot_pending", {})
        if not isinstance(pending, dict):
            return
        for task_id, item in pending.items():
            change = SnapshotChange(
                task_id=task_id,
                state=copy.deepcopy(item["state"]),
                mutations=tuple(item["mutations"]),
            )
            with self._lock:
                self._states[task_id] = copy.deepcopy(change.state)
                subscribers = tuple(self._subscribers.get(task_id, set()))
            for subscriber in subscribers:
                try:
                    subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, change)
                except RuntimeError:
                    log.warning(
                        "conversation_snapshot_subscriber_closed",
                        extra={
                            "msg": "快照订阅所在事件循环已关闭",
                            "data": {"task_id": task_id},
                        },
                    )

    def subscribe(self, task_id: int) -> tuple[asyncio.Queue[SnapshotChange], Callable[[], None]]:
        """注册一个 Task 订阅并返回队列与注销函数。"""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()
        subscriber = _Subscriber(loop, queue)
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(subscriber)

        def unsubscribe() -> None:
            """移除该订阅者，重复调用安全。"""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is None:
                    return
                subscribers.discard(subscriber)
                if not subscribers:
                    self._subscribers.pop(task_id, None)

        return queue, unsubscribe

    def read_or_initialize(
        self,
        task_id: int,
        fallback: ConversationStateSnapshot,
    ) -> ConversationStateSnapshot:
        """读取快照；缺失时以调用方提供的已投影 state 初始化持久化快照。"""

        current = self.load(task_id)
        if current is not None:
            return current
        current = self._crud.get_or_create(task_id, fallback)
        self.hydrate(task_id, current)
        return copy.deepcopy(current)


def _apply_mutation(
    state: ConversationStateSnapshot,
    mutation: ConversationStateMutation,
) -> None:
    """在 JSON state 上执行一个 set 或 append-text 操作。"""

    if not mutation.path:
        if mutation.kind != "set" or not isinstance(mutation.value, dict):
            raise ValueError("root mutation must be set with an object value")
        state_dict = cast(dict[str, object], state)
        state_dict.clear()
        state_dict.update(copy.deepcopy(mutation.value))
        return
    parent: object = state
    for key in mutation.path[:-1]:
        parent = parent[key]  # type: ignore[index]
    key = mutation.path[-1]
    if mutation.kind == "set":
        value = copy.deepcopy(mutation.value)
        if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value  # type: ignore[index]
        return
    if mutation.kind != "append-text" or not isinstance(mutation.value, str):
        raise ValueError("append-text mutation must carry a string")
    current = parent[key]  # type: ignore[index]
    if not isinstance(current, str):
        raise TypeError("append-text target must be a string")
    parent[key] = current + mutation.value  # type: ignore[index]
