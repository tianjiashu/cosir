# Task 5 — Final acceptance report

Date: 2026-09-12
Branch: `codex/conversation-runtime-rebuild`
Commit: `test: complete conversation state acceptance` (final commit is reported with the
verification handoff; the hash is intentionally not self-embedded because amending this report
changes the hash).

## Result

The conversation-state rebuild passes the Task 5 acceptance coverage in code. The full backend
suite has one remaining failure, classified as an environment-only missing artifact:
`apps/terminal-worker/target/debug/terminal-worker.exe`. No compatibility migration, legacy
snapshot fallback, persisted Transport snapshot, or snapshot test was added.

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
uv --cache-dir H:\coding-agent\.uv-cache-task5 run pytest -q --tb=short --basetemp H:\coding-agent\apps\backend\.pytest-basetemp-task5-full-final2
1 failed, 377 passed, 1 skipped in 9.73s
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

## Changed files in this acceptance commit

- `apps/backend/tests/test_task5_acceptance.py`
- `apps/backend/tests/test_tool_executor_pipeline.py`
- `apps/backend/app/assistant_transport/service/conversation_event_projector.py`
- `apps/backend/app/assistant_transport/service/conversation_task_state_service.py`
- `apps/backend/app/service/task/conversation_task_context_service.py`
- `apps/backend/app/core/tools/guard/tool_output_budget.py`
- this report

The worktree also contains unrelated pre-existing user changes and generated test scratch
directories; they are intentionally not included in this commit.

Independent subagent review could not be executed because this runtime exposes no subagent
dispatch tool. A local diff review and the verification above were completed; this limitation is
reported rather than presented as an independent review.
