"""In-process Assistant Transport state and canonical cold-read boundary."""

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
from app.assistant_transport.stream import SnapshotChange
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ConversationTaskStateService:
    """Own process-local Transport state and lazily rebuild it from canonical records.

    The working copy is intentionally ephemeral and mounted in the task's
    ``TaskRuntimeSpace``: projector mutations and subscriber changes never write a database
    row. The first read for a task loads one Task, all its Runs, and all context rows, then
    delegates the pure wire-state construction to ``ConversationTaskStateRebuilder``. Later
    reads reuse that task-local in-memory snapshot until an explicit rebuild or process reset.

    Parameters:
        task_source: Task CRUD-like source; defaults to the process dependency.
        run_source: Run CRUD-like source; defaults to the process dependency.
        context_source: Context CRUD-like source; defaults to the process dependency.

    Side effects:
        ``get_state`` may read the three canonical SQLite tables and caches only an in-memory
        copy. ``apply_planned`` and ``publish_state`` update process-local state and notify SSE
        subscribers. No method accesses checkpoints, tools, frontend runtime state, or persisted
        Transport snapshots.

    Concurrency:
        ``_lock`` (class-level ``RLock``) is the **sole serialization point** for every
        ``TaskRuntimeSpace`` snapshot working copy: lazy rebuild, projector mutation, publish,
        subscriber registration and deletion cleanup all pass through it. That is why the space
        itself carries no snapshot lock (see ``app.task_runtime.task_runtime_space`` module
        docstring). Direct space access is valid only single-threaded (state-lifecycle tests do
        so); a multi-threaded caller must serialize itself, and the preferred fix is to route it
        back through this service instead of re-adding a per-space lock. Reviewers must not treat
        the missing per-space lock as a defect.
    """

    _lock: ClassVar[RLock] = RLock()
    _subscribers: ClassVar[dict[int, set[Subscriber]]] = {}
    _deleted_task_ids: ClassVar[set[int]] = set()

    def __init__(
            self,
    ) -> None:
        """Bind the canonical record sources used by cold reads."""
        from app.service import depends
        self._task_source = depends.get_task_crud()
        self._run_source = depends.get_conversation_run_crud()
        self._context_source = depends.get_conversation_task_context_crud()

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """Return a validated task-local working copy, lazily rebuilding it on first access.

        A present ``TaskRuntimeSpace`` snapshot is returned without canonical reads. Database
        changes become visible through the normal projector/update path; ordinary reads do not
        rebuild or merge a second copy.

        Raises:
            KeyError: If the Task does not exist or was deleted in this process.
            sqlalchemy.exc.SQLAlchemyError: If a canonical source read fails.
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
    ) -> SnapshotChange:
        """Apply a live projector plan to process-local state and notify subscribers.

        If no working copy exists, the canonical cold-read boundary supplies the initial state.
        The planner and mutations run under the same lock as subscriber registration so a first
        SSE frame cannot race with a queued change.
        """
        task_id = event.task_id
        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_snapshot(lambda: self._rebuild(task_id))
            mutations = tuple(event.plan(state))
            for mutation in mutations:
                _apply_mutation(state, mutation)
            validate_snapshot(state)
            change = SnapshotChange(task_id, copy.deepcopy(state), mutations)
            if mutations:
                self._publish(change)
            return change

    def publish_state(
            self,
            task_id: int,
            state: ConversationStateSnapshot,
            mutations: tuple[ConversationStateMutation, ...] | None = None,
    ) -> SnapshotChange:
        """Publish a state whose canonical database transaction has already committed."""

        validate_snapshot(state)
        change = SnapshotChange(
            task_id,
            copy.deepcopy(state),
            mutations or (ConversationStateMutation("set", (), copy.deepcopy(state)),),
        )
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._publish(change)
        return change

    def subscribe_with_snapshot(
            self, task_id: int
    ) -> tuple[asyncio.Queue[SnapshotChange], Callable[[], None], ConversationStateSnapshot]:
        """Atomically register an SSE subscriber and return its first state frame."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SnapshotChange] = asyncio.Queue()
        subscriber = Subscriber(loop, queue)
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._subscribers.setdefault(task_id, set()).add(subscriber)
            try:
                initial = self.get_state(task_id)
            except Exception:
                self._subscribers[task_id].discard(subscriber)
                if not self._subscribers[task_id]:
                    self._subscribers.pop(task_id, None)
                raise

        def unsubscribe() -> None:
            """Remove this subscriber; repeated calls are safe."""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)

        return queue, unsubscribe, initial

    def mark_task_deleted(self, task_id: int) -> None:
        """Discard process-local state/subscribers after canonical Task deletion."""

        with self._lock:
            self._deleted_task_ids.add(task_id)
            self._subscribers.pop(task_id, None)
            space = task_runtime_spaces.get(task_id)
            if space is not None:
                space.unload_snapshot()

    @classmethod
    def clear_process_state(cls) -> None:
        """Discard all ephemeral process-local state.

        Process-lifecycle hook, not a persistence operation. Clears subscriber queues and
        deleted-task tombstones so a later storage re-initialization in the same interpreter
        does not inherit stale working copies from the previous backend lifetime.
        """

        with cls._lock:
            cls._subscribers.clear()
            cls._deleted_task_ids.clear()
        task_runtime_spaces.close()

    def is_task_deleted(self, task_id: int) -> bool:
        """Return whether this process has finalized deletion for the Task."""

        with self._lock:
            return task_id in self._deleted_task_ids

    def _ensure_not_deleted(self, task_id: int) -> None:
        """Reject reads or projections after process-local deletion cleanup."""

        if task_id in self._deleted_task_ids:
            raise KeyError(task_id)

    def _rebuild(self, task_id: int) -> ConversationStateSnapshot:
        """Read canonical records and invoke the pure state rebuilder with structured logs."""

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

    def _publish(self, change: SnapshotChange) -> None:
        """Install a working copy and enqueue the change for current subscribers."""

        task_runtime_spaces.get_or_create(change.task_id).replace_snapshot(change.state)
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


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """Apply the Transport set/append-text mutation to an in-memory state."""

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


__all__ = ["ConversationTaskStateService"]
