"""Process-local Child Agent session coordination.

The database remains the source of truth for Task and ConversationRun lifecycle.  This
module owns only the short-lived registry, mailbox, startup acknowledgement and waiter
notifications needed to operate a Child Agent while the backend process is alive.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.tool_models.child_agent_session_args import (
    ChildAgentPendingState,
    ChildAgentTerminalMessage,
    ChildAgentWaitResult,
)
from app.models import ConversationRunCommand, ConversationRunRecord, ConversationRunStatus
from app.service.child_agent.async_child_agent_wait_coordinator import (
    AsyncChildAgentWaitCoordinator,
    WaitQueryResult,
)
from app.service.child_agent.child_agent_session_error import ChildAgentSessionError

if TYPE_CHECKING:
    from app.service.task.conversation_run_service import ConversationRunService
    from app.service.task.conversation_run_state_service import ConversationRunStateService
    from app.service.terminal.terminal_session_service import TerminalSessionService
    from app.task_runtime.service.task_service import TaskService


CHILD_START_TIMEOUT_SECONDS = 2.0
MAX_PENDING_CHILD_MESSAGES = 64
MAX_FOLLOW_UP_RETRIES = 3
MAX_CLOSED_CHILD_TOMBSTONES = 1024


@dataclass(frozen=True)
class ChildAgentStartResult:
    """Stable locator returned after child execution has been registered."""

    child_task_id: int
    child_run_id: int
    generation: str
    reservation_acquired: bool


@dataclass(frozen=True)
class ChildAgentSendResult:
    """Acknowledgement for one idempotent mailbox message."""

    accepted_seq: int
    duplicate: bool = False


@dataclass(frozen=True)
class ChildAgentStatusResult:
    """Canonical child status plus process-local mailbox projection."""

    child_task_id: int
    child_run_id: int | None
    status: str | None
    final_output: str | None
    end_reason: str | None
    queued_message_count: int
    closing: bool


@dataclass(frozen=True)
class ChildAgentCloseResult:
    """Result of an idempotent close request."""

    child_task_id: int
    child_run_id: int | None
    status: str | None
    idempotent: bool


@dataclass
class _MailboxMessage:
    message_id: str
    message: str
    accepted_seq: int


@dataclass
class _Session:
    parent_task_id: int
    parent_run_id: int
    child_task_id: int
    active_run_id: int
    generation: str
    child_agent_id: str
    title: str
    run_callback: Callable[[ConversationRunRecord], Awaitable[None]]
    runtime_event_loop: asyncio.AbstractEventLoop
    provider_id: int | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    closing: bool = False
    next_message_seq: int = 0
    mailbox: deque[_MailboxMessage] = field(default_factory=deque)
    messages_by_id: dict[str, _MailboxMessage] = field(default_factory=dict)
    execution_task: asyncio.Task[None] | None = None
    follow_up_pending: bool = False
    follow_up_launch_inflight: bool = False
    follow_up_retry_count: int = 0


class ChildAgentSessionService:
    """Own Child Agent runtime sessions without duplicating Run lifecycle state.

    Task/Run services are injected explicitly.  ``start_child`` performs only the
    synchronous creation and bounded launch acknowledgement; child workflow completion
    remains owned by ``ConversationRunExecutor``.  Mailbox messages never mutate a child
    context directly.  ``read_wait`` performs canonical Run reads and returns a
    ``WaitQueryResult`` suitable for ``AsyncChildAgentWaitCoordinator``.
    """

    def __init__(
        self,
        *,
        task_service: TaskService,
        conversation_run_service: ConversationRunService,
        conversation_run_state_service: ConversationRunStateService,
        conversation_run_executor: Any | None = None,
        wait_coordinator: AsyncChildAgentWaitCoordinator | None = None,
        terminal_session_service: TerminalSessionService | None = None,
    ) -> None:
        self._tasks = task_service
        self._runs = conversation_run_service
        self._run_state = conversation_run_state_service
        self._executor = conversation_run_executor
        self._wait_coordinator = wait_coordinator or AsyncChildAgentWaitCoordinator()
        self._terminal = terminal_session_service
        self._lock = threading.RLock()
        self._sessions: dict[tuple[int, str], _Session] = {}
        self._by_child_task: dict[int, _Session] = {}
        self._creating_keys: set[tuple[int, str]] = set()
        self._closed_parent_runs: set[int] = set()
        self._closed_child_task_ids: set[int] = set()
        self._closed_child_task_order: deque[int] = deque()
        self._shutdown = False
        self._finalization_workers = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="child-agent-finalization",
        )

    @property
    def wait_coordinator(self) -> AsyncChildAgentWaitCoordinator:
        """Return the process-local async wait coordinator."""

        return self._wait_coordinator

    def set_conversation_run_executor(self, executor: Any) -> None:
        """Bind the canonical executor after composition-root construction."""

        with self._lock:
            self._executor = executor

    def start_child(
        self,
        *,
        parent_task_id: int,
        parent_run_id: int,
        workspace_id: int,
        child_agent_id: str,
        title: str,
        prompt: str,
        tool_call_id: str,
        run_callback: Callable[[ConversationRunRecord], Awaitable[None]],
        runtime_event_loop: asyncio.AbstractEventLoop,
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChildAgentStartResult:
        """Create a child Task/Run and wait only for executor registration.

        This method is intentionally synchronous because the current tool handler runs in
        a worker thread.  It never waits for child workflow completion.
        """

        if not runtime_event_loop.is_running():
            raise ChildAgentSessionError(
                "child_agent_start_failed", "runtime event loop is not running"
            )
        key = (parent_run_id, tool_call_id)
        with self._lock:
            if self._shutdown:
                raise ChildAgentSessionError(
                    "child_agent_start_failed", "child session service is shut down"
                )
            existing = self._sessions.get(key)
            if existing is not None:
                if existing.closing:
                    raise ChildAgentSessionError("child_agent_session_closed")
                return ChildAgentStartResult(
                    existing.child_task_id,
                    existing.active_run_id,
                    existing.generation,
                    True,
                )
            if key in self._creating_keys:
                raise ChildAgentSessionError("child_agent_start_failed", "child launch is pending")
            if self._active_count_locked(parent_run_id) >= self._max_concurrency():
                raise ChildAgentSessionError("child_agent_concurrency_exceeded")
            self._creating_keys.add(key)

        child_task = None
        try:
            child_task = self._tasks.get_or_create_task(
                workspace_id=workspace_id,
                title=title,
                task_type="delegation",
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
            )
            try:
                child_run = self._runs.create_run(
                    task_id=child_task.id,
                    agent_id=child_agent_id,
                    provider_id=provider_id,
                    model_name=model_name,
                    reasoning_effort=reasoning_effort,
                    run_command=ConversationRunCommand(display_text=prompt),
                )
            except BaseException:
                self._reclaim_unpaired_child_task(child_task.id)
                raise
        except BaseException as exc:
            with self._lock:
                self._creating_keys.discard(key)
            if isinstance(exc, ChildAgentSessionError):
                raise
            if isinstance(exc, Exception):
                raise ChildAgentSessionError("child_agent_start_failed") from exc
            raise

        parent_terminal = self._parent_run_is_terminal(parent_run_id)
        with self._lock:
            self._creating_keys.discard(key)
            parent_closed = self._shutdown or parent_run_id in self._closed_parent_runs
            parent_closed = parent_closed or parent_terminal
            if parent_closed:
                self._release_parent_close_marker_locked(parent_run_id)
        if parent_closed:
            self._settle_failed_run(child_run.id, "child_agent_parent_closed")
            raise ChildAgentSessionError("child_agent_session_closed")

        with self._lock:
            generation = f"{parent_run_id}:{child_task.id}:{child_run.id}"
            session = _Session(
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                child_task_id=child_task.id,
                active_run_id=child_run.id,
                generation=generation,
                child_agent_id=child_agent_id,
                title=title,
                run_callback=run_callback,
                runtime_event_loop=runtime_event_loop,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
            self._sessions[key] = session
            self._by_child_task[child_task.id] = session

        acknowledgement: Future[ChildAgentStartResult] = Future()
        launch: Future[None] | None = None
        try:
            launch = asyncio.run_coroutine_threadsafe(
                self._launch_child(key, session, acknowledgement), runtime_event_loop
            )
            return acknowledgement.result(timeout=CHILD_START_TIMEOUT_SECONDS)
        except Exception as exc:
            if launch is not None:
                launch.cancel()
            self._cleanup_failed_start(session, key, "child_agent_start_failed")
            log.error(
                "child_agent_start_failed",
                extra={
                    "msg": "Child Agent 启动确认失败，已收敛 child Run",
                    "data": {
                        "parent_run_id": parent_run_id,
                        "child_task_id": session.child_task_id,
                        "child_run_id": session.active_run_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            if isinstance(exc, ChildAgentSessionError):
                raise
            raise ChildAgentSessionError("child_agent_start_failed") from exc

    async def _launch_child(
        self,
        key: tuple[int, str],
        session: _Session,
        acknowledgement: Future[ChildAgentStartResult],
    ) -> None:
        """Claim a pending child and register its background execution."""

        try:
            with self._lock:
                self._assert_launch_open_locked(session, key, session.generation)
                generation = session.generation
            claimed = await asyncio.to_thread(
                self._run_state.claim_pending_run,
                session.active_run_id,
            )
            with self._lock:
                self._assert_launch_open_locked(session, key, generation)
                if claimed is None:
                    raise ChildAgentSessionError(
                        "child_agent_start_failed", "child Run is not pending"
                    )
                executor = self._executor
                if executor is None:
                    raise ChildAgentSessionError(
                        "child_agent_start_failed", "Run executor is not configured"
                    )
                # This is deliberately synchronous: close cannot interleave between the
                # generation check and executor registration while this short fence is held.
                execution = executor.start_registered(session.active_run_id, session.run_callback)
                self._assert_launch_open_locked(session, key, session.generation)
                session.execution_task = execution
                acknowledgement.set_result(
                    ChildAgentStartResult(
                        child_task_id=session.child_task_id,
                        child_run_id=session.active_run_id,
                        generation=session.generation,
                        reservation_acquired=True,
                    )
                )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self._cleanup_failed_start, session, key, "child_agent_start_failed"
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._cleanup_failed_start, session, key, "child_agent_start_failed"
            )
            if not acknowledgement.done():
                acknowledgement.set_exception(
                    exc
                    if isinstance(exc, ChildAgentSessionError)
                    else ChildAgentSessionError("child_agent_start_failed")
                )

    def _assert_launch_open_locked(
        self, session: _Session, key: tuple[int, str], generation: str
    ) -> None:
        """Reject a launch whose process-local close generation is no longer current."""

        if (
            self._shutdown
            or session.closing
            or session.generation != generation
            or self._sessions.get(key) is not session
        ):
            raise ChildAgentSessionError("child_agent_start_failed")

    def _assert_session_generation_locked(self, session: _Session, generation: str) -> None:
        """Recheck a follow-up generation while the session lock is held."""

        if (
            self._shutdown
            or session.closing
            or session.generation != generation
            or self._by_child_task.get(session.child_task_id) is not session
        ):
            raise ChildAgentSessionError("child_agent_start_failed")

    def _cleanup_failed_start(
        self, session: _Session, key: tuple[int, str], reason: str
    ) -> None:
        """Converge a failed child launch and remove its process-local registry entry."""

        with self._lock:
            session.closing = True
            session.generation = f"{session.generation}:closed"
            execution = session.execution_task
            session.execution_task = None
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
            if self._by_child_task.get(session.child_task_id) is session:
                self._by_child_task.pop(session.child_task_id, None)
        if execution is not None and not execution.done():
            with suppress(RuntimeError):
                session.runtime_event_loop.call_soon_threadsafe(execution.cancel)
        cancellation_registry.mark_cancelled(session.active_run_id)
        try:
            self._settle_failed_run(session.active_run_id, reason)
        finally:
            cancellation_registry.clear(session.active_run_id)

    def _reclaim_unpaired_child_task(self, child_task_id: int) -> None:
        """Delete a newly created delegation Task when its Run cannot be created.

        Task creation and Run creation are intentionally kept behind the existing synchronous
        services.  This compensating action is only used before a session is registered, so a
        failed Run insert cannot leave a visible orphan Task.  Deletion failures are logged with
        the task id and re-raised to preserve the original startup failure at the caller.
        """

        try:
            self._tasks.delete_task(child_task_id)
        except BaseException:
            log.exception(
                "child_agent_orphan_task_reclaim_failed",
                extra={
                    "msg": "Child Agent Run 创建失败后回收 delegation Task 失败",
                    "data": {"child_task_id": child_task_id},
                },
            )
            raise

    def _parent_run_is_terminal(self, parent_run_id: int) -> bool:
        """Read the canonical parent Run without holding the in-memory session lock."""

        try:
            return self._is_terminal(self._runs.get_run(parent_run_id))
        except KeyError:
            return True

    def _release_parent_close_marker_locked(self, parent_run_id: int) -> None:
        """Drop a close marker after no in-flight child fact creation can observe it."""

        if not any(key[0] == parent_run_id for key in self._creating_keys):
            self._closed_parent_runs.discard(parent_run_id)

    def _remember_closed_child_task_locked(self, child_task_id: int) -> None:
        """Keep a bounded idempotency tombstone after removing a closed session."""

        if child_task_id in self._closed_child_task_ids:
            return
        self._closed_child_task_ids.add(child_task_id)
        self._closed_child_task_order.append(child_task_id)
        while len(self._closed_child_task_order) > MAX_CLOSED_CHILD_TOMBSTONES:
            self._closed_child_task_ids.discard(self._closed_child_task_order.popleft())

    def send(
        self,
        *,
        parent_task_id: int,
        parent_run_id: int,
        child_task_id: int,
        message: str,
        message_id: str,
    ) -> ChildAgentSendResult:
        """Queue a FIFO message after ownership and closing-fence validation."""

        session = self._owned_session(parent_task_id, parent_run_id, child_task_id)
        latest = self._latest_run(self._runs.list_runs_for_task(child_task_id))
        schedule_follow_up = False
        with self._lock:
            if session.closing:
                raise ChildAgentSessionError("child_agent_session_closed")
            previous = session.messages_by_id.get(message_id)
            if previous is not None:
                if previous.message != message:
                    raise ChildAgentSessionError("child_agent_message_conflict")
                return ChildAgentSendResult(previous.accepted_seq, duplicate=True)
            if len(session.mailbox) >= MAX_PENDING_CHILD_MESSAGES:
                raise ChildAgentSessionError("child_agent_busy_or_queue_full")
            session.next_message_seq += 1
            item = _MailboxMessage(message_id, message, session.next_message_seq)
            session.mailbox.append(item)
            session.messages_by_id[message_id] = item
            if (
                latest is not None
                and self._is_terminal(latest)
                and not session.follow_up_pending
            ):
                session.follow_up_pending = True
                session.follow_up_retry_count = 0
                schedule_follow_up = True
            result = ChildAgentSendResult(item.accepted_seq)
        if not schedule_follow_up:
            # Canonical reads stay outside the in-memory lock.  This second read closes the
            # terminal-commit/send race: a finalization worker either wins the memory lock and
            # schedules the mailbox, or this read sees the committed terminal Run and does so.
            latest = self._latest_run(self._runs.list_runs_for_task(child_task_id))
            if latest is not None and self._is_terminal(latest):
                with self._lock:
                    if (
                        not session.closing
                        and not session.follow_up_pending
                        and self._by_child_task.get(child_task_id) is session
                    ):
                        session.follow_up_pending = True
                        session.follow_up_retry_count = 0
                        schedule_follow_up = True
        if schedule_follow_up:
            self._schedule_follow_up(session)
        return result

    def status(
        self, *, parent_task_id: int, parent_run_id: int, child_task_id: int
    ) -> ChildAgentStatusResult:
        """Read current status and final output from canonical child Run history."""

        child_task = self._owned_child_task(parent_task_id, parent_run_id, child_task_id)
        runs = self._runs.list_runs_for_task(child_task.id)
        latest = self._latest_run(runs)
        with self._lock:
            session = self._by_child_task.get(child_task_id)
            closing = (
                session.closing
                if session is not None
                else latest is None or self._is_terminal(latest)
            )
            queued = len(session.mailbox) if session is not None else 0
        return ChildAgentStatusResult(
            child_task_id=child_task_id,
            child_run_id=latest.id if latest is not None else None,
            status=latest.status if latest is not None else None,
            final_output=latest.final_output if latest is not None else None,
            end_reason=latest.end_reason if latest is not None else None,
            queued_message_count=queued,
            closing=closing,
        )

    def close(
        self, *, parent_task_id: int, parent_run_id: int, child_task_id: int
    ) -> ChildAgentCloseResult:
        """Set the closing fence and converge every active child Run idempotently."""

        child_task = self._owned_child_task(parent_task_id, parent_run_id, child_task_id)
        with self._lock:
            session = self._by_child_task.get(child_task_id)
            already_closed = (session is not None and session.closing) or (
                child_task_id in self._closed_child_task_ids
            )
            if session is not None:
                session.closing = True
                session.generation = f"{session.generation}:closed"
                session.mailbox.clear()
            self._remember_closed_child_task_locked(child_task_id)
        runs = self._runs.list_runs_for_task(child_task.id)
        latest = self._latest_run(runs)
        if latest is not None and not self._is_terminal(latest):
            cancellation_registry.mark_cancelled(latest.id)
            if self._terminal is not None:
                self._terminal.close_run_terminals(latest.id, reason="child_agent_closed")
            with self._lock:
                execution_task = session.execution_task if session is not None else None
            if execution_task is not None and not execution_task.done():
                with suppress(RuntimeError):
                    session.runtime_event_loop.call_soon_threadsafe(execution_task.cancel)
            self._cancel_canonical_run(latest.id, "child_agent_closed")
            latest = self._latest_run(self._runs.list_runs_for_task(child_task.id))
        self._wait_coordinator.close_session(parent_run_id)
        with self._lock:
            if session is not None:
                for key, registered in tuple(self._sessions.items()):
                    if registered is session:
                        self._sessions.pop(key, None)
                        break
                if self._by_child_task.get(child_task_id) is session:
                    self._by_child_task.pop(child_task_id, None)
        return ChildAgentCloseResult(
            child_task_id=child_task_id,
            child_run_id=latest.id if latest is not None else None,
            status=self._safe_status(latest),
            idempotent=already_closed,
        )

    def close_children(self, parent_run_id: int) -> None:
        """Close a Run's direct children and its own Child session, if any.

        The second part is intentional: the same method is used by the explicit Run cancel
        boundary, so cancelling a Child Run must fence that Child session before queued
        mailbox work can create a follow-up Run.
        """

        with self._lock:
            self._closed_parent_runs.add(parent_run_id)
            own_session = next(
                (
                    session
                    for session in self._by_child_task.values()
                    if session.active_run_id == parent_run_id
                ),
                None,
            )
        if own_session is not None:
            try:
                self.close(
                    parent_task_id=own_session.parent_task_id,
                    parent_run_id=own_session.parent_run_id,
                    child_task_id=own_session.child_task_id,
                )
            except ChildAgentSessionError:
                log.exception(
                    "child_agent_run_finalization_close_failed",
                    extra={
                        "msg": "Child Run 取消/收尾时设置 session closing fence 失败",
                        "data": {"child_run_id": parent_run_id},
                    },
                )
        parent_task_id = self._parent_task_id_for_run(parent_run_id)
        if parent_task_id is None:
            with self._lock:
                self._release_parent_close_marker_locked(parent_run_id)
            return
        for child in self._tasks.list_child_tasks(parent_task_id, parent_run_id):
            try:
                self.close(
                    parent_task_id=parent_task_id,
                    parent_run_id=parent_run_id,
                    child_task_id=child.id,
                )
            except ChildAgentSessionError:
                log.exception(
                    "child_agent_parent_finalization_close_failed",
                    extra={
                        "msg": "父 Run 收尾关闭 child 失败",
                        "data": {"parent_run_id": parent_run_id},
                    },
                )
        with self._lock:
            self._release_parent_close_marker_locked(parent_run_id)

    def close_for_task_ids(self, task_ids: set[int]) -> None:
        """Fence and close live sessions whose Child Task rows are being deleted."""

        with self._lock:
            sessions = tuple(
                session for task_id, session in self._by_child_task.items() if task_id in task_ids
            )
        for session in sessions:
            try:
                self.close(
                    parent_task_id=session.parent_task_id,
                    parent_run_id=session.parent_run_id,
                    child_task_id=session.child_task_id,
                )
            except ChildAgentSessionError:
                log.exception(
                    "child_agent_task_cleanup_failed",
                    extra={
                        "msg": "Task 删除前关闭 Child Agent session 失败",
                        "data": {"child_task_id": session.child_task_id},
                    },
                )

    def cancel_descendants(self, parent_run_id: int) -> None:
        """Mark cancellation for the child Task tree using Task/Run ownership facts."""

        parent_task_id = self._parent_task_id_for_run(parent_run_id)
        if parent_task_id is None:
            return
        queue = deque(self._tasks.list_child_tasks(parent_task_id, parent_run_id))
        visited: set[int] = set()
        while queue:
            child = queue.popleft()
            if child.id in visited:
                continue
            visited.add(child.id)
            for run in self._runs.list_runs_for_task(child.id):
                if not self._is_terminal(run):
                    cancellation_registry.mark_cancelled(run.id)
            queue.extend(
                self._tasks.list_child_tasks(child.id, child.parent_run_id or parent_run_id)
            )

    def notify_waiters(self, parent_run_id: int) -> None:
        """Notify async waiters after canonical Run commit."""

        self._wait_coordinator.notify(parent_run_id)

    def notify_child_finalized(self, record: ConversationRunRecord) -> None:
        """Notify the parent and schedule one queued follow-up after child finalization."""

        child_task = self._tasks.get_task(record.task_id)
        if child_task.parent_run_id is None or child_task.parent_task_id is None:
            return
        try:
            self.notify_waiters(child_task.parent_run_id)
        except BaseException as exc:
            log.error(
                "child_agent_wait_notification_failed",
                extra={
                    "msg": "Child Agent finalization 的 wait 通知失败，继续处理 follow-up",
                    "data": {
                        "parent_run_id": child_task.parent_run_id,
                        "child_task_id": child_task.id,
                        "child_run_id": record.id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )
        with self._lock:
            session = self._by_child_task.get(child_task.id)
            if session is None or session.closing or session.active_run_id != record.id:
                return
            if not session.mailbox or session.follow_up_pending:
                return
            session.follow_up_pending = True
            session.follow_up_retry_count = 0
        try:
            self._schedule_follow_up(session)
        except BaseException as exc:
            with self._lock:
                session.follow_up_launch_inflight = False
            log.error(
                "child_agent_follow_up_schedule_failed",
                extra={
                    "msg": (
                        "Child Agent finalization 的 follow-up 调度失败，"
                        "保留 pending 供 sweep 重试"
                    ),
                    "data": {
                        "parent_run_id": child_task.parent_run_id,
                        "child_task_id": child_task.id,
                        "child_run_id": record.id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )

    def sweep_pending_follow_ups(self, *, max_items: int = MAX_FOLLOW_UP_RETRIES) -> int:
        """Re-submit bounded follow-up launches whose runtime callback previously failed.

        The sweep is deliberately finite per call and leaves a still-pending mailbox visible
        to a later parent-finalization or shutdown cleanup.  It never creates a Run after the
        session closing fence has been set.
        """

        if max_items < 1:
            return 0
        with self._lock:
            pending = [
                session
                for session in self._by_child_task.values()
                if (
                    session.follow_up_pending
                    and not session.closing
                    and session.follow_up_retry_count < MAX_FOLLOW_UP_RETRIES
                )
            ][:max_items]
        for session in pending:
            self._schedule_follow_up(session)
        if pending:
            log.info(
                "child_agent_follow_up_sweep",
                extra={
                    "msg": "Child Agent follow-up 已进入有界重试 sweep",
                    "data": {"session_count": len(pending)},
                },
            )
        return len(pending)

    def on_run_finalized(self, record: ConversationRunRecord) -> None:
        """Queue canonical finalization work on a dedicated worker thread."""

        with self._lock:
            if self._shutdown:
                return
        try:
            future = self._finalization_workers.submit(self._process_run_finalized, record)
            future.add_done_callback(
                lambda completed: self._log_finalization_result(completed, record)
            )
        except RuntimeError:
            log.warning(
                "child_agent_finalization_after_shutdown",
                extra={
                    "msg": "Child Agent finalization worker 已关闭，跳过 live cleanup",
                    "data": {},
                },
            )

    @staticmethod
    def _log_finalization_result(future: Future[Any], record: ConversationRunRecord) -> None:
        """Consume a finalization worker Future and log unexpected failures structurally."""

        try:
            future.result()
        except BaseException as exc:
            log.error(
                "child_agent_finalization_worker_failed",
                extra={
                    "msg": "Child Agent finalization worker 异常结束，wait/follow-up 可能未完成",
                    "data": {
                        "run_id": record.id,
                        "task_id": record.task_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )

    def _process_run_finalized(self, record: ConversationRunRecord) -> None:
        """Perform all Task/Run reads and parent/child cleanup off the event loop."""

        try:
            child_task = self._tasks.get_task(record.task_id)
        except KeyError:
            return
        if child_task.parent_run_id is not None and child_task.parent_task_id is not None:
            self.notify_child_finalized(record)
            return
        self.notify_waiters(record.id)
        try:
            self.close_children(record.id)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                return
            log.exception(
                "child_agent_parent_finalization_close_failed",
                extra={
                    "msg": "父 Run worker 收尾关闭 child 失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )

    def read_wait(
        self,
        *,
        parent_task_id: int,
        parent_run_id: int,
        targets: list[dict[str, int | None]] | None,
        wait_mode: str,
    ) -> WaitQueryResult:
        """Return a canonical wait predicate and final-output projection."""

        direct_children = self._tasks.list_child_tasks(parent_task_id, parent_run_id)
        if targets is None:
            target_list = [
                {"child_task_id": child.id, "after_run_id": None} for child in direct_children
            ]
        else:
            target_list = targets
        if not target_list:
            raise ChildAgentSessionError("child_agent_no_targets")
        if wait_mode not in {"any", "all"}:
            raise ChildAgentSessionError("child_agent_invalid_wait_mode")
        seen: set[int] = set()
        resolved: list[tuple[Any, int | None]] = []
        for target in target_list:
            child_task_id = target.get("child_task_id")
            if not isinstance(child_task_id, int) or child_task_id in seen:
                raise ChildAgentSessionError("child_agent_duplicate_target")
            seen.add(child_task_id)
            child = self._owned_child_task(parent_task_id, parent_run_id, child_task_id)
            after_run_id = target.get("after_run_id")
            runs = self._runs.list_runs_for_task(child.id)
            if after_run_id is not None and all(run.id != after_run_id for run in runs):
                raise ChildAgentSessionError("child_agent_invalid_cursor")
            resolved.append((child, after_run_id))

        messages: list[ChildAgentTerminalMessage] = []
        message_order: dict[int, tuple[Any, int, int]] = {}
        pending: list[ChildAgentPendingState] = []
        for child, after_run_id in resolved:
            runs = self._runs.list_runs_for_task(child.id)
            candidate = self._first_terminal_after(runs, after_run_id)
            if candidate is None:
                latest = self._latest_run(runs)
                pending.append(
                    ChildAgentPendingState(
                        child_task_id=child.id,
                        status=latest.status if latest is not None else None,
                    )
                )
            else:
                messages.append(
                    ChildAgentTerminalMessage(
                        child_task_id=child.id,
                        child_run_id=candidate.id,
                        status=candidate.status,
                        final_output=candidate.final_output,
                        end_reason=candidate.end_reason,
                    )
                )
                message_order[candidate.id] = self._run_order_key(candidate, child.id)
        ready = (
            bool(messages) if wait_mode == "any" else not pending and len(messages) == len(resolved)
        )
        if wait_mode == "any" and ready:
            messages.sort(key=lambda message: message_order[message.child_run_id])
            messages = messages[:1]
            pending = [
                ChildAgentPendingState(child_task_id=child.id, status=None)
                for child, _ in resolved
                if child.id != messages[0].child_task_id
            ]
        result = ChildAgentWaitResult(timed_out=False, messages=messages, pending=pending)
        return WaitQueryResult(ready=ready, value=result)

    def snapshot_wait_targets(
        self, *, parent_task_id: int, parent_run_id: int
    ) -> list[dict[str, int | None]]:
        """Snapshot direct Child Agent targets once at wait invocation time.

        The returned list is a caller-owned locator snapshot.  It contains no runtime
        handles or lifecycle state, and later Child Task creation cannot expand the current
        wait operation.
        """

        children = self._tasks.list_child_tasks(parent_task_id, parent_run_id)
        return [{"child_task_id": child.id, "after_run_id": None} for child in children]

    def close_active_run(self, run_id: int, reason: str = "user_cancelled") -> None:
        """Close the Child Agent session whose active canonical Run is ``run_id``.

        This is the explicit Stop boundary for a Child Run.  It sets the session closing
        fence before the Run cancellation path can observe a late mailbox message, so a
        queued follow-up cannot be created after the user has stopped the Child Agent.
        """

        with self._lock:
            session = next(
                (candidate for candidate in self._by_child_task.values()
                 if candidate.active_run_id == run_id),
                None,
            )
        if session is None:
            return
        self.close(
            parent_task_id=session.parent_task_id,
            parent_run_id=session.parent_run_id,
            child_task_id=session.child_task_id,
        )

    def shutdown(self) -> Any:
        """Fence all sessions, cancel active Runs, and wake waiters."""

        with self._lock:
            self._shutdown = True
            sessions = tuple(self._by_child_task.values())
        for session in sessions:
            try:
                self.close(
                    parent_task_id=session.parent_task_id,
                    parent_run_id=session.parent_run_id,
                    child_task_id=session.child_task_id,
                )
            except Exception:
                log.exception(
                    "child_agent_shutdown_close_failed",
                    extra={
                        "msg": "backend shutdown 关闭 child 失败",
                        "data": {"child_task_id": session.child_task_id},
                    },
                )
        wait_shutdown = self._wait_coordinator.shutdown()
        self._finalization_workers.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._sessions.clear()
            self._by_child_task.clear()
            self._creating_keys.clear()
            self._closed_parent_runs.clear()
            self._closed_child_task_ids.clear()
            self._closed_child_task_order.clear()
        return wait_shutdown

    def _owned_session(
        self, parent_task_id: int, parent_run_id: int, child_task_id: int
    ) -> _Session:
        self._owned_child_task(parent_task_id, parent_run_id, child_task_id)
        with self._lock:
            session = self._by_child_task.get(child_task_id)
        if session is None:
            raise ChildAgentSessionError("child_agent_session_closed")
        return session

    def _owned_child_task(self, parent_task_id: int, parent_run_id: int, child_task_id: int) -> Any:
        try:
            child = self._tasks.get_task(child_task_id)
        except KeyError as exc:
            raise ChildAgentSessionError("child_agent_not_found") from exc
        try:
            parent = self._tasks.get_task(parent_task_id)
        except KeyError as exc:
            raise ChildAgentSessionError("child_agent_not_owned") from exc
        if (
            child.parent_task_id != parent_task_id
            or child.parent_run_id != parent_run_id
            or child.workspace_id != parent.workspace_id
            or child.task_type != "delegation"
        ):
            raise ChildAgentSessionError("child_agent_not_owned")
        return child

    def _active_count_locked(self, parent_run_id: int) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if session.parent_run_id == parent_run_id and not session.closing
        )

    @staticmethod
    def _max_concurrency() -> int:
        from app.config.settings import Settings

        return Settings.DELEGATION_MAX_CONCURRENCY

    def _cancel_canonical_run(self, run_id: int, reason: str) -> None:
        self._run_state.cancel_run_if_running(run_id, end_reason=reason)

    def _settle_failed_run(self, run_id: int, reason: str) -> None:
        """Cancel a failed-start Run, falling back to canonical failure if publication fails."""

        try:
            self._cancel_canonical_run(run_id, reason)
        except Exception:
            log.exception(
                "child_agent_start_cancel_failed",
                extra={
                    "msg": "Child Agent 启动失败的 cancel 收口失败，尝试 canonical fail",
                    "data": {"child_run_id": run_id, "reason": reason},
                },
            )
            try:
                self._run_state.fail_run_if_running(run_id, end_reason=reason)
            except Exception:
                log.exception(
                    "child_agent_start_fail_failed",
                    extra={
                        "msg": "Child Agent 启动失败的 canonical fail 也失败",
                        "data": {"child_run_id": run_id, "reason": reason},
                    },
                )

    @staticmethod
    def _is_terminal(run: Any) -> bool:
        return run.status in {
            ConversationRunStatus.COMPLETED.value,
            ConversationRunStatus.FAILED.value,
            ConversationRunStatus.CANCELLED.value,
        }

    @staticmethod
    def _safe_status(run: Any | None) -> str | None:
        return run.status if run is not None else None

    @staticmethod
    def _latest_run(runs: list[Any]) -> Any | None:
        return max(runs, key=lambda run: ChildAgentSessionService._run_order_key(run), default=None)

    @staticmethod
    def _run_order_key(run: Any, child_task_id: int | None = None) -> tuple[Any, int, int]:
        """Order canonical child Run history deterministically for wait-any selection."""

        return (
            getattr(run, "created_at", None),
            run.id,
            child_task_id or getattr(run, "task_id", 0),
        )

    def _first_terminal_after(self, runs: list[Any], after_run_id: int | None) -> Any | None:
        ordered = sorted(runs, key=self._run_order_key)
        if after_run_id is None:
            candidates = ordered
        else:
            cursor_index = next(
                index for index, run in enumerate(ordered) if run.id == after_run_id
            )
            candidates = ordered[cursor_index + 1 :]
        return next((run for run in candidates if self._is_terminal(run)), None)

    def _parent_task_id_for_run(self, parent_run_id: int) -> int | None:
        try:
            parent_run = self._runs.get_run(parent_run_id)
            task = self._tasks.get_task(parent_run.task_id)
        except KeyError:
            return None
        return task.id

    def _schedule_follow_up(self, session: _Session) -> None:
        async def launch() -> None:
            created_run: Any | None = None
            try:
                with self._lock:
                    if session.closing or not session.mailbox:
                        session.follow_up_pending = False
                        session.follow_up_launch_inflight = False
                        return
                    generation = session.generation
                    messages = tuple(session.mailbox)
                    prompt = "\n".join(item.message for item in messages)
                    self._assert_session_generation_locked(session, generation)
                created_run = await asyncio.to_thread(
                    self._runs.create_run,
                    task_id=session.child_task_id,
                    agent_id=session.child_agent_id,
                    provider_id=session.provider_id,
                    model_name=session.model_name,
                    reasoning_effort=session.reasoning_effort,
                    run_command=ConversationRunCommand(display_text=prompt),
                )
                with self._lock:
                    self._assert_session_generation_locked(session, generation)
                claimed = await asyncio.to_thread(
                    self._run_state.claim_pending_run, created_run.id
                )
                with self._lock:
                    self._assert_session_generation_locked(session, generation)
                    if claimed is None:
                        raise ChildAgentSessionError("child_agent_start_failed")
                    executor = self._executor
                    if executor is None:
                        raise ChildAgentSessionError("child_agent_start_failed")
                    # Registration has no await point.  The session fence therefore prevents
                    # close from observing an unregistered active Run.
                    execution = executor.start_registered(created_run.id, session.run_callback)
                    self._assert_session_generation_locked(session, generation)
                    for item in messages:
                        if session.mailbox and session.mailbox[0] is item:
                            session.mailbox.popleft()
                    session.active_run_id = created_run.id
                    session.generation = f"{generation}:followup"
                    session.execution_task = execution
                    session.follow_up_pending = bool(session.mailbox)
                    session.follow_up_retry_count = 0
            except asyncio.CancelledError:
                await self._fail_follow_up(session, created_run)
                raise
            except Exception:
                await self._fail_follow_up(session, created_run)
                log.exception(
                    "child_agent_follow_up_start_failed",
                    extra={
                        "msg": "Child Agent follow-up 启动失败",
                        "data": {"child_task_id": session.child_task_id},
                    },
                )

        with self._lock:
            if session.closing or not session.mailbox or session.follow_up_launch_inflight:
                return
            session.follow_up_launch_inflight = True
        try:
            future = asyncio.run_coroutine_threadsafe(launch(), session.runtime_event_loop)
            future.add_done_callback(self._log_follow_up_result)
        except RuntimeError:
            with self._lock:
                session.follow_up_launch_inflight = False
            log.warning(
                "child_agent_follow_up_runtime_unavailable",
                extra={
                    "msg": (
                        "Child Agent follow-up runtime 不可用，保留 pending 供 bounded sweep 收口"
                    ),
                    "data": {"child_task_id": session.child_task_id},
                },
            )

    async def _fail_follow_up(self, session: _Session, created_run: Any | None) -> None:
        """Converge a failed follow-up Run while retaining its mailbox messages."""

        if created_run is not None:
            await asyncio.to_thread(
                self._settle_failed_run,
                created_run.id,
                "child_agent_start_failed",
            )
        with self._lock:
            session.follow_up_launch_inflight = False
            if session.closing or self._by_child_task.get(session.child_task_id) is not session:
                session.follow_up_pending = False
                return
            session.follow_up_pending = bool(session.mailbox)
            session.follow_up_retry_count += 1
            if session.follow_up_retry_count >= MAX_FOLLOW_UP_RETRIES:
                log.warning(
                    "child_agent_follow_up_retry_exhausted",
                    extra={
                        "msg": "Child Agent follow-up 有界重试耗尽，保留 mailbox 等收口",
                        "data": {
                            "child_task_id": session.child_task_id,
                            "retry_count": session.follow_up_retry_count,
                        },
                    },
                )

    @staticmethod
    def _log_follow_up_result(future: Future[Any]) -> None:
        """Consume the cross-thread future so unexpected task errors remain observable."""

        try:
            future.result()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            log.error(
                "child_agent_follow_up_callback_failed",
                extra={
                    "msg": "Child Agent follow-up 后台任务以异常结束",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )
