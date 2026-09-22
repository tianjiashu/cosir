# Task 2 Final Acceptance Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove synchronous SQLite/resource cleanup from backend event-loop paths and make Child Agent finalization failures observable without changing frontend contracts.

**Architecture:** Keep short process-local registration/fencing sections synchronous and await blocking canonical reads/writes or resource cleanup through `asyncio.to_thread`. Reuse the canonical terminal update record for post-commit finalization notification, and attach structured callbacks to every finalization Future.

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy-backed services, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Backend only; no frontend changes.
- Preserve API contracts, shutdown ordering, and original startup/shutdown exceptions.
- Do not commit git changes.
- Use structured backend logging for cleanup/finalization failures.

### Task 1: Executor canonical I/O boundaries

**Files:**
- Modify: `apps/backend/app/assistant_transport/service/conversation_run_executor.py`
- Modify: `apps/backend/app/service/task/conversation_run_state_service.py`
- Test: `apps/backend/tests/test_conversation_run_executor_cancel.py`
- Test: `apps/backend/tests/test_task2_acceptance_async_boundaries.py`

- [ ] Add failing async tests that block `get_run` and prove the loop heartbeat continues for executor start, execute, cancel, and tool-call cancel; add a finalization test proving no synchronous post-write read is performed.
- [ ] Run the focused tests and confirm they fail for the expected blocking/read behavior.
- [ ] Move canonical executor reads and projector read paths to `asyncio.to_thread`, leaving registration/fencing code free of await points.
- [ ] Make finalization observer calls consume the canonical updated record returned by the conditional write.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Async lifespan cleanup

**Files:**
- Modify: `apps/backend/app/lifespan.py`
- Test: `apps/backend/tests/test_task2_lifecycle_cleanup.py`

- [ ] Add failing heartbeat tests around startup-failure and normal shutdown dependency cleanup.
- [ ] Run the tests and confirm synchronous cleanup blocks the loop before the fix.
- [ ] Await blocking cleanup through `asyncio.to_thread` while preserving cleanup order and exception behavior.
- [ ] Run lifecycle tests and confirm they pass.

### Task 3: Child finalization Future observability

**Files:**
- Modify: `apps/backend/app/service/child_agent/child_agent_session_service.py`
- Test: `apps/backend/tests/test_task2_acceptance_async_boundaries.py`

- [ ] Add a failing test for a finalization worker Future that raises and verify a structured error log is emitted.
- [ ] Add a failing test showing child finalization keeps wait notification and follow-up scheduling failures observable and retryable.
- [ ] Attach done callbacks, consume Future results, and isolate/log child wait/follow-up branch failures.
- [ ] Run child-session and race tests.

### Task 4: Verification and report

**Files:**
- Create: `task-2-acceptance-fix-report.md`

- [ ] Run Task1, Task2, race/lifecycle/executor tests.
- [ ] Run Ruff and `compileall` for backend.
- [ ] Review diff to ensure only backend plus requested report/plan changed and no git commit was created.
- [ ] Record evidence, known baseline failures, and scope in the acceptance report.
