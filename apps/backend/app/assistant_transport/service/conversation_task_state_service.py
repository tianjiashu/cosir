"""In-process Assistant Transport state and canonical cold-read boundary."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Any, ClassVar, Protocol, cast

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationStateRebuildError,
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import SnapshotChange
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord


class _TaskSource(Protocol):
    def get(self, task_id: int) -> TaskRecord: ...


class _RunSource(Protocol):
    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]: ...


class _ContextSource(Protocol):
    def get(
        self, task_id: int, include_in_context: bool = True
    ) -> list[ConversationTaskContextRecord]: ...


class ConversationTaskStateService:
    """Own process-local Transport state and rebuild it from canonical records when cold.

    The working copy is intentionally ephemeral: projector mutations and subscriber changes
    never write a database row. A cold read loads one Task, all its Runs, and all context rows,
    then delegates the pure wire-state construction to ``ConversationTaskStateRebuilder``.

    Parameters:
        task_source: Task CRUD-like source; defaults to the process dependency.
        run_source: Run CRUD-like source; defaults to the process dependency.
        context_source: Context CRUD-like source; defaults to the process dependency.

    Side effects:
        ``get_state`` may read the three canonical SQLite tables and caches only an in-memory
        copy. ``apply_planned`` and ``publish_state`` update process-local state and notify SSE
        subscribers. No method accesses checkpoints, tools, frontend runtime state, or persisted
        Transport snapshots.
    """

    _lock: ClassVar[RLock] = RLock()
    _states: ClassVar[dict[int, ConversationStateSnapshot]] = {}
    _live_message_mutations: ClassVar[dict[int, list[ConversationStateMutation]]] = {}
    _subscribers: ClassVar[dict[int, set[Subscriber]]] = {}
    _deleted_task_ids: ClassVar[set[int]] = set()

    def __init__(
        self,
        *,
        task_source: _TaskSource | None = None,
        run_source: _RunSource | None = None,
        context_source: _ContextSource | None = None,
    ) -> None:
        """Bind the canonical record sources used by cold reads."""

        if task_source is None or run_source is None or context_source is None:
            from app.service import depends

            task_source = task_source or depends.get_task_crud()
            run_source = run_source or depends.get_conversation_run_crud()
            context_source = context_source or depends.get_conversation_task_context_crud()
        self._task_source = task_source
        self._run_source = run_source
        self._context_source = context_source

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """Return a validated working copy, rebuilding it when this process has no copy.

        A present working copy is merged with a canonical rebuild only at a read boundary. Run
        lifecycle, Run usage, Task current Run, and Task context-window fields always come from
        canonical records; active in-memory message deltas remain available to live SSE.

        Raises:
            KeyError: If the Task does not exist or was deleted in this process.
            ConversationStateRebuildError: If canonical records cannot form valid wire state.
            sqlalchemy.exc.SQLAlchemyError: If a canonical source read fails.
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            working = self._states.get(task_id)
            if working is None:
                state = self._rebuild(task_id)
            else:
                canonical = self._rebuild(task_id)
                state = self._merge_canonical_facts(
                    working,
                    canonical,
                    tuple(self._live_message_mutations.get(task_id, ())),
                )
            validate_snapshot(state)
            self._states[task_id] = copy.deepcopy(state)
            return copy.deepcopy(state)

    async def read(self, task_id: int) -> ConversationStateSnapshot:
        """Read state without blocking the event loop; never starts or resumes a Run."""

        return await asyncio.to_thread(self.get_state, task_id)

    def rebuild_state(self, task_id: int) -> ConversationStateSnapshot:
        """Force a canonical rebuild and install it as the new process-local working copy.

        This is used after edit/fork database commits where old in-memory message parts must not
        survive as the new Transport baseline.
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            state = self._rebuild(task_id)
            self._states[task_id] = copy.deepcopy(state)
            self._live_message_mutations.pop(task_id, None)
            return copy.deepcopy(state)

    def apply_planned(
        self,
        task_id: int,
        planner: Callable[[ConversationStateSnapshot], Sequence[ConversationStateMutation]],
    ) -> SnapshotChange:
        """Apply a live projector plan to process-local state and notify subscribers.

        If no working copy exists, the canonical cold-read boundary supplies the initial state.
        The planner and mutations run under the same lock as subscriber registration so a first
        SSE frame cannot race with a queued change.
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            existing = self._states.get(task_id)
            state = copy.deepcopy(existing if existing is not None else self._rebuild(task_id))
            mutations = tuple(planner(copy.deepcopy(state)))
            for mutation in mutations:
                _apply_mutation(state, mutation)
            validate_snapshot(state)
            change = SnapshotChange(task_id, copy.deepcopy(state), mutations)
            self._states[task_id] = copy.deepcopy(state)
            live_mutations = self._live_message_mutations.setdefault(task_id, [])
            live_mutations.extend(
                mutation
                for mutation in mutations
                if _is_live_message_mutation(state, mutation)
            )
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
            self._live_message_mutations.pop(task_id, None)
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
            self._states.pop(task_id, None)
            self._live_message_mutations.pop(task_id, None)
            self._subscribers.pop(task_id, None)

    @classmethod
    def clear_process_state(cls) -> None:
        """Discard all ephemeral state when the backend storage lifecycle ends.

        This is a process-lifecycle hook, not a persistence operation. It prevents a later
        storage initialization in the same interpreter (notably test/application reloads) from
        inheriting working copies or deleted-task tombstones from the previous backend lifetime.
        """

        with cls._lock:
            cls._states.clear()
            cls._live_message_mutations.clear()
            cls._subscribers.clear()
            cls._deleted_task_ids.clear()

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
            try:
                context_rows = self._context_source.get(task_id, include_in_context=False)
            except (KeyError, TypeError, ValueError) as exc:
                raise ConversationStateRebuildError(
                    "malformed_context_record",
                    "canonical context record could not be deserialized",
                    task_id=task_id,
                ) from exc
            state = ConversationTaskStateRebuilder.rebuild(task, runs, context_rows)
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

    @staticmethod
    def _merge_canonical_facts(
        working: ConversationStateSnapshot,
        canonical: ConversationStateSnapshot,
        live_mutations: Sequence[ConversationStateMutation],
    ) -> ConversationStateSnapshot:
        """Make canonical messages authoritative while retaining only active stream deltas.

        Canonical state is always the base. User messages, completed AI/tool facts, and every
        lifecycle/usage field therefore cannot be replaced by stale memory. The only memory data
        admitted back is an assistant message delta produced by a live projector mutation for an
        active Run: a missing assistant part is appended, and a longer text value is accepted only
        when it is a strict prefix extension of the canonical text. Non-prefix stale text and
        changes to an existing tool part are discarded deterministically.
        """

        state = copy.deepcopy(canonical)
        if not live_mutations:
            return state

        working_runs = {run["runId"]: run for run in working["runs"]}
        for canonical_run in state["runs"]:
            if canonical_run["status"] in {"completed", "failed", "cancelled"}:
                continue
            working_run = working_runs.get(canonical_run["runId"])
            if working_run is None:
                continue
            canonical_assistant = next(
                (
                    message
                    for message in canonical_run["messages"]
                    if message["role"] == "assistant"
                ),
                None,
            )
            working_assistant = next(
                (
                    message
                    for message in working_run["messages"]
                    if message["role"] == "assistant"
                ),
                None,
            )
            if working_assistant is None:
                continue
            if canonical_assistant is None:
                canonical_run["messages"].append(copy.deepcopy(working_assistant))
                continue
            _merge_live_assistant_parts(canonical_assistant, working_assistant)
        state["current_run_id"] = canonical["current_run_id"]
        state["context_usage_ratio"] = canonical["context_usage_ratio"]
        state["context_usage_used"] = canonical["context_usage_used"]
        state["context_window_total"] = canonical["context_window_total"]
        return state

    def _publish(self, change: SnapshotChange) -> None:
        """Install a working copy and enqueue the change for current subscribers."""

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


def _is_live_message_mutation(
    state: ConversationStateSnapshot,
    mutation: ConversationStateMutation,
) -> bool:
    """Return whether a mutation targets an active assistant message in the working state."""

    path = mutation.path
    if (
        len(path) < 4
        or path[0] != "runs"
        or not isinstance(path[1], int)
        or path[2] != "messages"
        or not isinstance(path[3], int)
    ):
        return False
    try:
        role = state["runs"][path[1]]["messages"][path[3]]["role"]
    except (IndexError, KeyError, TypeError):
        return False
    return role == "assistant"


def _merge_live_assistant_parts(
    canonical_assistant: dict[str, Any],
    working_assistant: dict[str, Any],
) -> None:
    """Merge only prefix-extending live assistant parts into canonical message facts."""

    canonical_parts = cast(list[dict[str, Any]], canonical_assistant["parts"])
    working_parts = cast(list[dict[str, Any]], working_assistant["parts"])
    for live_part in working_parts:
        if not isinstance(live_part, dict):
            continue
        part_type = live_part.get("type")
        if part_type == "tool-call":
            call_id = live_part.get("toolCallId")
            if not isinstance(call_id, str) or any(
                part.get("type") == "tool-call" and part.get("toolCallId") == call_id
                for part in canonical_parts
            ):
                continue
            canonical_parts.append(copy.deepcopy(live_part))
            continue
        if part_type not in {"text", "reasoning"}:
            continue
        matching = next(
            (
                part
                for part in canonical_parts
                if part.get("type") == part_type
            ),
            None,
        )
        if matching is None:
            canonical_parts.append(copy.deepcopy(live_part))
            continue
        canonical_text = matching.get("text")
        live_text = live_part.get("text")
        if (
            isinstance(canonical_text, str)
            and isinstance(live_text, str)
            and len(live_text) > len(canonical_text)
            and live_text.startswith(canonical_text)
        ):
            matching["text"] = live_text
            live_status = live_part.get("status")
            if isinstance(live_status, str):
                matching["status"] = live_status


__all__ = ["ConversationTaskStateService"]
