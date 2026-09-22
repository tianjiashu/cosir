"""Async wait coordination for canonical Child Agent terminal-state reads.

The coordinator owns only process-local waiter signals.  It does not own Run state,
perform blocking waits, or persist a cursor.  A caller supplies a short synchronous
canonical query; the query runs on a bounded private executor and returns a
``WaitQueryResult``.  Notifications may originate on any thread and are marshalled to
the backend event loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings


@dataclass(frozen=True)
class WaitQueryResult:
    """Result of one short canonical read used by the wait predicate."""

    ready: bool
    value: Any = None


@dataclass(frozen=True)
class WaitOutcome:
    """Terminal outcome of a bounded wait."""

    value: Any = None
    timed_out: bool = False
    interrupted_by: str | None = None


@dataclass
class _Signal:
    version: int = 0
    interrupted_by: str | None = None
    waiters: dict[int, asyncio.Event] = field(default_factory=dict)


class AsyncChildAgentWaitCoordinator:
    """Coordinate non-blocking async waits over short canonical state queries.

    A waiter is registered before its first query.  Every notification increments a
    monotonic per-parent version and sets each waiter's private event.  The waiter
    compares the version after every query before awaiting, which closes the
    notification-before-await and query-race lost-wakeup windows.
    """

    def __init__(self, *, max_waiters: int | None = None, read_workers: int = 4) -> None:
        if max_waiters is None:
            max_waiters = Settings.MAX_CONCURRENT_CHILD_WAITS
        if max_waiters < 1:
            raise ValueError("max_waiters must be greater than zero")
        if read_workers < 1:
            raise ValueError("read_workers must be greater than zero")
        self._max_waiters = max_waiters
        self._read_executor = ThreadPoolExecutor(
            max_workers=read_workers,
            thread_name_prefix="child-agent-wait-read",
        )
        self._signals: dict[int, _Signal] = {}
        self._state_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_waiters = 0
        self._next_waiter_id = 0
        self._shutdown = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the event loop currently owning the waiter registry."""

        return self._loop

    async def wait_async(
        self,
        parent_run_id: int,
        canonical_query: Callable[[], WaitQueryResult],
        *,
        timeout_seconds: float,
    ) -> WaitOutcome:
        """Wait for a canonical query predicate without blocking the event loop.

        ``canonical_query`` must be a short synchronous read.  It runs in the
        coordinator's private executor; it must not itself wait for a notification.
        Cancellation and query exceptions propagate after the waiter is removed.
        """

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if inspect.iscoroutinefunction(canonical_query):
            raise TypeError("canonical_query must be synchronous")

        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._loop is None:
                self._loop = loop
            elif self._loop is not loop:
                raise RuntimeError("wait coordinator cannot be shared across event loops")
            if self._shutdown:
                return WaitOutcome(interrupted_by="shutdown")
            if self._active_waiters >= self._max_waiters:
                raise WaiterCapacityExceeded("child_agent_wait_concurrency_exceeded")
            signal = self._signals.setdefault(parent_run_id, _Signal())
            self._active_waiters += 1
            self._next_waiter_id += 1
            waiter_id = self._next_waiter_id
            waiter_event = asyncio.Event()
            signal.waiters[waiter_id] = waiter_event
            version_before = signal.version

        deadline = loop.time() + timeout_seconds
        latest: WaitQueryResult | None = None
        try:
            while True:
                with self._state_lock:
                    interrupted_by = signal.interrupted_by
                    shutdown = self._shutdown
                if interrupted_by is not None:
                    return WaitOutcome(
                        value=latest.value if latest is not None else None,
                        interrupted_by=interrupted_by,
                    )
                if shutdown:
                    return WaitOutcome(
                        value=latest.value if latest is not None else None,
                        interrupted_by="shutdown",
                    )

                latest = await loop.run_in_executor(self._read_executor, canonical_query)
                if not isinstance(latest, WaitQueryResult):
                    raise TypeError("canonical_query must return WaitQueryResult")
                if latest.ready:
                    return WaitOutcome(value=latest.value)

                with self._state_lock:
                    interrupted_by = signal.interrupted_by
                    current_version = signal.version
                if interrupted_by is not None:
                    return WaitOutcome(value=latest.value, interrupted_by=interrupted_by)
                if self._shutdown:
                    return WaitOutcome(value=latest.value, interrupted_by="shutdown")
                if current_version != version_before:
                    version_before = current_version
                    continue

                remaining = deadline - loop.time()
                if remaining <= 0:
                    return WaitOutcome(value=latest.value, timed_out=True)
                try:
                    await asyncio.wait_for(waiter_event.wait(), timeout=remaining)
                except TimeoutError:
                    return WaitOutcome(value=latest.value, timed_out=True)
                waiter_event.clear()
                with self._state_lock:
                    version_before = signal.version
        finally:
            with self._state_lock:
                signal.waiters.pop(waiter_id, None)
                self._active_waiters -= 1
                if not signal.waiters and self._signals.get(parent_run_id) is signal:
                    self._signals.pop(parent_run_id, None)

    def notify(self, parent_run_id: int) -> None:
        """Schedule a monotonic notification for all waiters of one parent Run."""

        self._schedule_signal(parent_run_id, None)

    def interrupt(self, parent_run_id: int, reason: str) -> None:
        """Wake parent waiters with a structured interruption reason."""

        self._schedule_signal(parent_run_id, reason)

    def cancel_parent(self, parent_run_id: int) -> None:
        """Interrupt waits because the parent Run was cancelled."""

        self.interrupt(parent_run_id, "parent_cancelled")

    def close_session(self, parent_run_id: int) -> None:
        """Interrupt waits because the Child session is closing."""

        self.interrupt(parent_run_id, "session_closed")

    def shutdown(self) -> _CompletedAwaitable:
        """Wake every waiter and stop accepting reads during backend shutdown."""

        with self._state_lock:
            self._shutdown = True
            parent_run_ids = tuple(self._signals)
        for parent_run_id in parent_run_ids:
            self._schedule_signal(parent_run_id, "shutdown")
        # Reads are deliberately short and already bounded by the reader contract.  Let
        # queued reads drain so a waiter can observe ``_shutdown`` and return its
        # structured interruption instead of receiving an executor-generated cancellation.
        self._read_executor.shutdown(wait=False)
        return _CompletedAwaitable()

    close = shutdown

    def _schedule_signal(self, parent_run_id: int, reason: str | None) -> None:
        """Queue a signal on the owner loop without touching asyncio objects off-loop."""

        with self._state_lock:
            if parent_run_id not in self._signals:
                return
            loop = self._loop

        if loop is not None and not loop.is_closed():
            try:
                # ``call_soon_threadsafe`` is safe both while the loop is running and
                # while it is temporarily stopped; the callback runs when the owner
                # resumes the loop.  It also closes the check-then-set race at shutdown.
                loop.call_soon_threadsafe(self._bump_signal, parent_run_id, reason)
                return
            except RuntimeError:
                # The loop can close after the check.  Preserve the monotonic state for
                # cleanup/diagnostics, but never call Event.set from this thread.
                pass

        with self._state_lock:
            signal = self._signals.get(parent_run_id)
            if signal is not None:
                signal.version += 1
                if reason is not None:
                    signal.interrupted_by = reason

    def _bump_signal(self, parent_run_id: int, reason: str | None) -> None:
        """Apply a queued notification on the event-loop owner thread."""

        with self._state_lock:
            signal = self._signals.get(parent_run_id)
            if signal is None:
                return
            signal.version += 1
            if reason is not None:
                signal.interrupted_by = reason
            for waiter_event in tuple(signal.waiters.values()):
                waiter_event.set()


class WaiterCapacityExceeded(RuntimeError):
    """Raised when the bounded async waiter capacity is exhausted."""


class _CompletedAwaitable:
    """Allow synchronous lifecycle callers and async tests to share shutdown()."""

    def __await__(self):
        if False:
            yield None
        return None
