# Task 2 lifecycle fix report

## Scope

- Backend lifecycle and related backend tests only.
- No frontend changes made by this fix.
- No Git commit created.

## P1 closure

1. Close/follow-up fencing now uses `closing` plus session generation checks. The launch path rechecks before/after canonical claim and before executor start; the session lock spans the create/claim/start phases so close cannot slip between the final check and start. A stale or failed launch is canonical-cancelled/failed and never started.
2. Follow-up messages remain in the FIFO mailbox until claim and executor registration succeed. Created follow-up Runs are canonically cancelled, with canonical fail fallback if cancellation publication fails. Failed attempts remain available to bounded retry/sweep; failed initial starts remove both session indexes and clear cancellation signals.
3. `send()` re-reads terminal child state while holding the session lock used by finalization. The send/finalization race therefore either observes the terminal Run or observes an already-pending follow-up, and schedules at most one launch.
4. Async launch, follow-up create/claim/canonical convergence, executor shutdown, and parent close cleanup use `asyncio.to_thread` for synchronous canonical operations. Event-loop execution is reserved for executor task registration and in-memory coordination.
5. `start_child` and executor-start failures converge the Run, cancel any registered execution task, remove session indexes, and clear the cancellation registry.
6. `ConversationRunExecutor` now requires the explicit child-session contract. The old `AttributeError`/`suppress` fallback and legacy descendant traversal are removed; missing wiring is exposed as an exception.

## P2 closure

- `reset_service_dependencies()` clears the Child Agent wait coordinator and session-service `lru_cache` entries.
- The unreachable post-return legacy path in `DelegationExecutor` and its dead helpers/imports were removed.
- wait-any ordering is `(created_at, id, child_task_id)`.
- Added wait-all coverage and explicit rejection of non-`delegation` child tasks.

## RED evidence

The new regression tests were run before their corresponding production fixes:

```text
tests/test_task2_child_agent_sessions.py -k '...'
2 failed, 4 passed, 7 deselected
```

The expanded RED set additionally failed on missing child-session contract and dependency-cache reset:

```text
4 failed, 4 passed, 7 deselected
```

The send/finalization race test independently failed before the lock-protected canonical re-read:

```text
test_send_rechecks_terminal_child_after_finalization_race: FAILED
```

## Final verification

All commands below were run after the final changes.

```text
./.venv/bin/pytest -q tests/test_task1_async_tools_and_child_wait.py tests/test_task2_child_agent_sessions.py
34 passed in 3.54s

./.venv/bin/pytest -q tests/test_conversation_run_executor_cancel.py tests/test_run_failure_settlement.py tests/test_run_zombie_convergence.py tests/test_child_agent_claim.py tests/test_child_agent_claim_adversarial.py tests/test_delegate_task_model_contract.py tests/test_delegate_task_runtime_projection_adversarial.py tests/test_tool_call_lifecycle.py tests/test_tool_observation_summary.py
114 passed in 1.22s

./.venv/bin/pytest -q tests/test_task2_child_agent_sessions.py tests/test_child_agent_claim.py tests/test_child_agent_claim_adversarial.py tests/test_delegation_model_inheritance.py
51 passed in 3.44s

./.venv/bin/ruff check app/service/child_agent/child_agent_session_service.py app/assistant_transport/service/conversation_run_executor.py app/service/depends.py app/core/delegation/delegation_executor.py tests/test_task2_child_agent_sessions.py tests/test_child_agent_claim_adversarial.py tests/test_conversation_run_executor_cancel.py tests/test_run_zombie_convergence.py
All checks passed!

./.venv/bin/python -m compileall -q app
exit_code=0
```

`git status --short` remains dirty because the workspace contained prior Task 1/Task 2 and other uncommitted work; no commit or frontend edit was made during this fix.
