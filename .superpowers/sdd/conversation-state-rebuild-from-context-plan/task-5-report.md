# Task 5 — Final acceptance report

Date: 2026-09-12
Branch: `codex/conversation-runtime-rebuild`
Commit: `fix: close Task 5 final review blockers` (final hash is reported with the verification
handoff; it is intentionally not self-embedded because amending this report changes the hash).

## Result

The independent final-review blockers are covered by deterministic regression tests and pass in
code. The full backend suite has one remaining failure, classified as an environment-only
missing artifact: `apps/terminal-worker/target/debug/terminal-worker.exe`. No compatibility
migration, legacy snapshot fallback, persisted Transport snapshot, or snapshot test was added.

## Acceptance coverage

Added `apps/backend/tests/test_task5_acceptance.py` with nine acceptance tests using real SQLite,
SQLAlchemy sessions, and CRUD/service boundaries where practical. The tests cover:

- fresh schema/model/production-tree checks for the absence of the old conversation snapshot
  surface, while retaining file snapshot storage;
- cold Transport and Agent-context reconstruction for empty tasks, multiple AI/tool steps,
  success/error/cancelled tool results, malformed metadata, and canonical Context-row IDs;
- DB-first terminal Run facts and convergence after injected post-commit projector failure;
- serialized SSE first-frame behavior, process-local projector updates, no snapshot-row writes,
  and disconnect without cancellation/replay;
- same-task command idempotency and mutual exclusion, plus concurrent independent tasks;
- real SQLite fork/edit/reset identity and checkpoint isolation;
- bounded restart/orphan recovery, interrupted-tool repair, no implicit replay, and stale
  generation suppression after restart.

An additional regression test in `tests/test_tool_executor_pipeline.py` covers nullable
`ToolObservation.content` through the output budget.

## TDD fixes found during acceptance

1. Tool denial/invalid-argument paths crashed because nullable observation content reached the
   output budget. Red: three existing pipeline tests reproduced `TypeError` and the new nullable
   regression test failed. Green: `4 passed`; the budget now normalizes missing content to `""`.
2. Edit/restart deleted Context rows without flushing an externally managed, `autoflush=False`
   session, so the same transaction's idempotency read suppressed the replacement user row.
   The real SQLite fork/edit acceptance test failed before the fix and passed after an explicit
   flush at the service boundary.
3. An old `ConversationTaskStateService` generation could mutate the new process-local state
   after restart. The stale-projector acceptance test failed before the fix and passed after
   generation ownership checks and projector suppression were added.

## TDD fixes from the independent final-review round

RED was captured before the implementation changes:

```text
uv --cache-dir H:\coding-agent\.uv-cache-task5 run pytest tests/test_task5_final_review_regressions.py -q --tb=short --basetemp H:\coding-agent\apps\backend\.pytest-basetemp-final-review-red3
7 failed
```

The failures reproduced the six requested blockers, including the cold-rebuild
`duplicate_tool_call_id` error and the deterministic generation race. After each fix, the final
regression suite is GREEN:

```text
uv --cache-dir H:\coding-agent\.uv-cache-task5 run pytest tests/test_task5_final_review_regressions.py -q --tb=short --basetemp H:\coding-agent\apps\backend\.pytest-basetemp-final-review-green-final
8 passed in 0.98s
```

The fix round makes Run terminal facts commit before tool-settlement projection, swallows
projector failures at the workflow/settlement boundary, rechecks generation while holding the
publication lock, removes the duplicate direct-create user event (including child/delegation
creation), scopes tool-call matching by run, reads the persisted task context window, and clears
stale usage/error JSON on resume.

## Verification evidence

Focused baseline before Task 5 additions:

```text
129 passed
```

Task 5 acceptance after fixes:

```text
9 passed in 2.86s
```

Final full backend run, with the workspace cache/temp workaround:

```text
uv --cache-dir H:\coding-agent\.uv-cache-task5 run pytest -q --tb=short --basetemp H:\coding-agent\apps\backend\.pytest-basetemp-final-review-full-final
1 failed, 388 passed, 1 skipped in 9.99s
```

The sole failure is `tests/test_terminal_worker_integration.py::test_real_worker_powershell_command_exits`:
the desktop-provided `terminal-worker.exe` is absent and process creation raises `WinError 2`
as `TerminalWorkerUnavailableError`. This is not a conversation-state or test-code failure.

Static checks:

- Ruff on all changed production/test files: passed (`All checks passed!`).
- `python -m compileall -q app`: passed.
- Forbidden production-reference scan for `conversation_task_snapshots`,
  `ConversationTaskSnapshot`, `conversation_task_snapshot`, `state_json`, and
  `ensure_state_snapshot`: clean.
- `git diff --check`: passed.
- Strict mypy: not clean due 117 pre-existing repository-wide errors in 47 files. The earlier
  nullable-content and generation-assignment errors introduced by this acceptance work were
  removed; remaining errors include existing Transport TypedDict, storage, logging, workflow,
  and unrelated tooling issues.

## Changed files in this final-review fix commit

- `apps/backend/tests/test_task5_final_review_regressions.py`
- `apps/backend/tests/test_task3_canonical_write_paths.py`
- `apps/backend/tests/test_apply_patch_tool_errors.py`
- `apps/backend/tests/test_workflow_operations_tool_message.py`
- `apps/backend/app/assistant_transport/service/conversation_run_executor.py`
- `apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py`
- `apps/backend/app/assistant_transport/service/conversation_task_state_service.py`
- `apps/backend/app/core/delegation/delegation_executor.py`
- `apps/backend/app/core/tools/schemas/tool_observation.py`
- `apps/backend/app/core/tools/tool_execute/tool_cancelled.py`
- `apps/backend/app/core/tools/tool_execute/tool_error.py`
- `apps/backend/app/core/tools/tool_execute/tool_handler_runner.py`
- `apps/backend/app/core/tools/tool_execute/tool_success.py`
- `apps/backend/app/core/tools/tool_handler/apply_patch_tool.py`
- `apps/backend/app/core/tools/tool_contract.md`
- `apps/backend/app/core/workflows/workflow_operations.py`
- `apps/backend/app/service/task/conversation_run_service.py`
- `apps/backend/app/storage/crud/conversation_run_crud.py`
- `apps/backend/app/task_runtime/service/task_service.py`
- this report

The preceding acceptance commit contains the original acceptance tests and baseline fixes. The
worktree also contains unrelated pre-existing progress/plan/backup changes and generated test
scratch directories; they are intentionally not included in this commit.
