# Task 2 — Child Agent Session lifecycle report

## Scope

Implemented backend-only Child Agent session lifecycle integration. No frontend/Tauri files
were changed, and no Git commit was created.

## Implemented

- Fixed Task 1 tool-level async cancellation: `ToolCallCancelled` is converted to a cancelled
  `ToolObservation` and returned to the parent workflow; run-level cancellation still propagates.
- Wired the production `AsyncChildAgentWaitCoordinator` and canonical Child Run reader through
  the service composition root, with finalization notifications and shutdown cleanup.
- Changed `delegate_task` to create Task/Run/session references and launch through the existing
  backend event loop and `ConversationRunExecutor` without awaiting Child completion.
- Added `child_agent_send`, `child_agent_status`, `child_agent_wait`, and `child_agent_close`
  with parent Task/Run/workspace ownership checks, FIFO mailbox/idempotency, canonical status and
  `final_output` reads, any/all waits, at-least-once cursors, and stable errors.
- Added close fences, canonical Run cancellation, parent finalization observer handling, bounded
  follow-up retry/sweep, startup orphan recovery, and task-deletion cleanup hooks.
- Added `ReactGraphState.child_agents` as a locator-only checkpoint projection. Runtime tasks,
  mailboxes, futures, clients, and context state are not checkpoint data; resumed delegate calls
  replay the stored locator instead of creating a second child.
- Kept the old delegation storage path only as compatibility scaffolding; the new Child Agent
  lifecycle uses Task/ConversationRun facts as canonical state.

## TDD evidence

Initial RED checks were run before implementation:

- `./.venv/bin/pytest -q tests/test_task2_child_agent_sessions.py` failed during collection because
  `ChildAgentSessionService` did not exist.
- The focused async cancellation regression failed with `asyncio.CancelledError` escaping from
  `WorkflowOperations` instead of returning a cancelled observation.

## Verification

Passing focused and adjacent backend command:

```text
./.venv/bin/pytest -q tests/test_task1_async_tools_and_child_wait.py tests/test_task2_child_agent_sessions.py tests/test_conversation_run_executor_cancel.py tests/test_run_failure_settlement.py tests/test_child_agent_claim.py tests/test_child_agent_claim_adversarial.py tests/test_delegate_task_model_contract.py tests/test_delegate_task_runtime_projection_adversarial.py tests/test_tool_call_lifecycle.py tests/test_tool_observation_summary.py
127 passed in 1.23s
```

Additional focused result after the final session/recovery tests:

```text
./.venv/bin/pytest -q tests/test_task2_child_agent_sessions.py tests/test_task1_async_tools_and_child_wait.py
23 passed in 0.89s
```

Static/runtime checks:

```text
./.venv/bin/python -m compileall -q app
./.venv/bin/ruff check <Task 2 changed backend files>
All checks passed
```

## Pre-existing/out-of-scope failures

The broader adjacent command was also run and reported `225 passed, 1 failed, 8 errors`.
The failures were outside this Task 2 implementation:

- `tests/test_lifespan_split_adversarial.py`: fixture setup imports missing `app.hook`.
- `tests/test_run_failure_convergence_adversarial.py::test_terminal_error_cancelled_fallback_code`:
  existing failure-catalog constant mismatch.
- `tests/test_task5_acceptance.py::test_real_sqlite_same_task_is_mutually_exclusive_and_same_command_is_idempotent`:
  existing SQLite command uniqueness race (`conversation_commands.task_id, command_id`).
