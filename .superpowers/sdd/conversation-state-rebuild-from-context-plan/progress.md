# SDD ledger — plan: docs/plan/conversation-state-rebuild-from-context-plan.md

## Setup

- Workspace: current checkout `H:\coding-agent`, branch `codex/conversation-runtime-rebuild`.
- The checkout is not a linked git worktree, but it is a dedicated feature branch and the user
  explicitly requested implementation in this shared workspace; no branch switch or worktree
  creation was performed.
- The provided POSIX `sdd-workspace`/`task-brief` scripts could not run because Windows Bash/WSL
  is unavailable to the sandbox. The directory and briefs are maintained manually under this
  plan's `.superpowers/sdd/` workspace.
- Existing unrelated change preserved: `apps/backend/app/assistant_transport/request/assistant_transport_request.py~`.

## Preflight scan

| Task | Own consistency check | Shared interface / dependency | Finding and ruling |
| --- | --- | --- | --- |
| Task 1 — facts/schema | Model fields, records, CRUD and fresh schema tests must agree | Task/Run/Context records consumed by Tasks 2–5 | Ruling: Task 1 owns durable field names and serialization shapes; later tasks consume only its tests and records. |
| Task 2 — rebuilders | Rebuilder accepts only three model records and returns validated wire state | Consumes Task 1 model/record shapes; consumed by Tasks 4–5 | Ruling: checkpoint, ToolRegistry and memory state are excluded from cold rebuild; malformed target-schema data fails explicitly. |
| Task 3 — canonical writes | Context writes and Run terminal writes precede memory Transport updates | Produces rows consumed by Task 2; uses Task 1 fields | Ruling: no transaction spans database commit and in-memory snapshot mutation; tests must assert ordering. |
| Task 4 — transport/lifecycle | Snapshot persistence removal must not remove live SSE working state or explicit checkpoint continuation | Consumes Tasks 1–3; touches projector, read paths, fork/edit/restart | Ruling: checkpoint remains control-only for explicit continuation; it is never a snapshot/context source. |
| Task 5 — acceptance | Fresh fixtures must cover the full plan without compatibility behavior | Verifies all prior tasks and frontend wire contract | Ruling: greenfield schema only; missing metadata is a failure, not a legacy fallback. |
| Task 1 ↔ Task 2 | Task 1 creates fields Task 2 reads | `ConversationTaskContextRecord`, `ConversationRunRecord`, `TaskRecord` | No contradiction; Task 2 is blocked until target serialization shape is defined. |
| Task 2 ↔ Task 3 | Task 3 must write every field Task 2 requires | Context metadata and Run usage/error | No contradiction; Task 3 must not rely on projector-only fields. |
| Task 3 ↔ Task 4 | Task 4 removes snapshot writes while Task 3 still updates live state | Projector/Transport state service | No contradiction; database-first ordering is the shared invariant. |
| Task 4 ↔ Task 5 | Task 5 tests the final no-snapshot runtime | Schema registration and all read/write call sites | No contradiction; Task 5 is the final gate, not a compatibility layer. |

No plan defect prevents starting Task 1. All production changes must follow the TDD red-green-refactor
cycle and each task must receive an independent review before the next task.

## Task 1

- BASE: `ffbd35d218b3d1e72bb422f96dea34f10f5bf0c3`
- Implementer: Helmholtz (`01a09115-8ffc-7071-8d1e-da5aa30dd804`)
- Implementer commit: `3585cbbcd26d1b282e6776ff10852e06aac15956`
- Status: DONE_WITH_CONCERNS; task reviewer Feynman (`01a09122-bf65-79d0-9531-10e86856e373`) dispatched.
- Concern carried forward: fresh schema no longer creates snapshot table, while old TaskService deletion
  still references snapshot CRUD; this is explicitly assigned to Task 4 and is not a compatibility fix.
- Review verdict: Spec compliance ❌; Task quality ❌.
- Review findings: usage nullability is too permissive; metadata/error JSON is not structurally typed;
  context fork does not clone metadata/version; TDD RED evidence and malformed-input coverage are
  incomplete.
- Ruling: keep the existing TaskService snapshot cleanup failure assigned to Task 4 — Task 1 owns the
  target fresh schema and must not reintroduce a snapshot table or widen its scope into lifecycle cleanup;
  cost if wrong: the full suite remains red until Task 4 removes that stale dependency.
- Fix round 1/5 dispatched to the original implementer for the four in-scope findings.
- Fix round 1 result: commit `131fb6f`; usage/fork/TDD findings addressed, metadata contract remains
  open because nullable nested fields and broad tool_result/error message validation still fail review.
- Fix round 2/5 dispatched to the original implementer for the remaining metadata/error findings.
- Fix round 2 result: commit `4dbe153`; implementer reports RED `14 failed, 23 passed` plus 2
  supplemental failures, GREEN `38 passed`, related regression `46 passed`, Ruff clean.
- Task 1 round-2 scoped re-review dispatched to Feynman with the `131fb6f..4dbe153` diff.
- Round-2 re-review: usage nullability, metadata/tool_result strictness and error message boundaries
  ADDRESSED; review found an Important integration risk because persisted tool-call metadata requires
  normalized fields while existing Transport events allow omitted args/presentation/isError.
- Ruling: Task 1 must expose and test the persistence normalization contract without changing wire
  schema; Task 3 must call it in the canonical writer. Cost if wrong: a valid no-args tool call could
  fail to persist or make cold rebuild impossible.
- Fix round 3/5 dispatched to the original implementer for the normalization contract.
- Fix round 3 result: commit `644860e`; RED `1 failed, 3 passed`, GREEN `42 passed`, related
  regression `50 passed`, Ruff clean. Normalization helper added and Task 3 integration requirement
  recorded in the report.
- Final scoped re-review for Task 1 dispatched to Feynman with the `4dbe153..644860e` diff.
- Task 1: complete (commits `ffbd35d..644860e`, review clean after 3 fix rounds).

## Task 2

- Brief: `.superpowers/sdd/conversation-state-rebuild-from-context-plan/task-2-brief.md`.
- Implementer: Arendt (`01a0913c-5c41-7f01-96d1-f234f4d58ac4`), commit `16b08ede337d557f969c8159a3c577ef101c78db`.
- Implementer status: DONE_WITH_CONCERNS; related tests `88 passed`, focused MyPy/Ruff passed; full
  suite `311 passed, 1 skipped, 7 unrelated failures`.
- Task reviewer: Faraday (`01a09147-5929-7862-9541-37061d37f462`) dispatched with diff
  `644860e..16b08ed`.
- Task-2 review verdict: spec compliance PASS for the narrow brief; task quality FAIL.
- Review findings: terminal Runs leave unmatched tool calls pending/running; tool-result full errors
  are mapped into the UI error field instead of the controlled short `status_hint`; semantically
  misplaced metadata is silently dropped; display data is not defensively copied; coverage misses
  success/cancelled/status_hint and malformed semantic combinations.
- Rulings for fix round: terminal `cancelled` maps unmatched calls to `cancelled`, terminal
  `completed`/`failed` maps them to `failed`; UI error uses controlled `status_hint` (full error stays
  model/context data); row/message-type metadata mismatches fail explicitly; rebuilt display data is
  deep-copied. Add regression tests before production changes.
- Task-2 fix round 1/5 dispatched to the original implementer; re-review is required before Task 3.
- Fix round 1 result: commit `9e75a9e`; RED `11 failed, 14 passed`, focused GREEN `25 passed`,
  related regression `99 passed`, Ruff/focused MyPy passed.
- Task-2 round-1 re-review: Spec compliance ❌; Task quality ❌. The original findings were addressed,
  but failure/cancelled `display_data` is not constrained to the error contract, Human/System text or
  reasoning parts are still silently dropped, and an AI-row `errorCode` can survive when the Tool row
  omits it.
- Ruling: reject/normalize success-shaped display data on failed/cancelled tool results according to
  the existing tool UI contract; reject every non-empty UI part on Human/System rows; make Tool result
  metadata the sole source of `errorCode` and clear stale AI values. Add tests first, including hint
  length/shape and terminal unmatched cases where relevant.
- Task-2 fix round 2/5 dispatched to the original implementer; re-review remains required.
- Fix round 2 result: implementation commit `f06dffb`; report commit `f49af77`; RED `9 failures`
  (then 1 additional invalid-hint failure), focused GREEN `29 passed`, related regression `113 passed`,
  Ruff/focused MyPy passed.
- Round-2 implementation changed the rebuilder, tool error classification, focused tests and report;
  Task 3, snapshot removal, canonical writers, API/SSE and Projector remain untouched.
- Task-2 round-2 re-review dispatched to Faraday; Task 2 remains open pending clean verdict.
- Task-2 round-2 re-review: Spec compliance PASS; Task quality PASS; no Important/Minor findings.
  Task 2 is complete. Reviewer note: report header still says DONE_WITH_CONCERNS and should be
  normalized when convenient; it does not affect implementation compliance.

## Task 3

- Brief: `.superpowers/sdd/conversation-state-rebuild-from-context-plan/task-3-brief.md`.
- Implementer: Locke (`01a0915f-770c-73e2-8dd6-825e61a7e85a`).
- Implementer commit: `718d4911c3d589d4169a81eaeb573bc52b83f626`.
- TDD/verification reported by implementer: focused `37 passed`; Ruff, compileall and diff checks
  passed; full backend `336 passed, 1 skipped, 7 failed` (known snapshot-boundary and
  terminal-worker environment failures).
- Task 3 changed canonical context/system/user/AI/tool writes, Run usage/error/final-output writes,
  and Task usage ordering; it did not switch Task 4 snapshot read/removal paths.
- Status: implementation complete, independent review pending; Task 4 remains blocked until clean.
- Task 3 review verdict: Spec compliance FAIL; Task quality FAIL.
- Independent review findings: model chunk loses interleaved tool-call order and structured/complete
  AI message semantics; post-commit projector/transport failures can abort Agent execution; Tool
  settle is only read-before-insert without durable exactly-once or duplicate-event suppression;
  restart recovery does not repair interrupted tool calls or publish terminal events; fresh Run setup
  deletes and recreates the already persisted initial HumanMessage; failed tool events can expose
  unsanitized success-shaped display data.
- Ruling: keep DB failures fatal but isolate post-commit projector/transport failures; preserve actual
  stream order and serializer-compatible AI fields; add a durable unique tool-result invariant and
  return a created/duplicate outcome so only the first result emits; make restart recovery persist
  interrupted-tool repairs and terminal fields before events; preserve the initial user row across
  fresh startup; sanitize event payloads identically to persisted failure metadata. Add integration
  or real-CRUD tests for race/idempotence/recovery where feasible, not only mocks.
- Task 3 fix round 1/5 dispatched to Locke; independent re-review required.

### Task 3 — fix round 1

- Strict TDD RED: focused Task 3/context tests reported `7 failed, 51 passed`; the additional
  created-vs-duplicate settle test independently reported `1 failed` before implementation.
- GREEN: focused Task 3/context tests `60 passed`; related regression `51 passed, 1 known Task 4
  snapshot assertion failed`; full backend with workspace basetemp `344 passed, 1 skipped, 7 known
  failures` (snapshot cleanup/read-path boundary plus missing terminal-worker sidecar).
- Fixes: preserve streamed tool positions and full LangChain AI semantics; make post-commit listener/
  terminal-event failures non-fatal; enforce durable tool-call uniqueness with created-vs-duplicate
  outcomes; transactionally recover Run and interrupted ToolMessage facts before projector; preserve
  fresh-start HumanMessage row identity; share sanitized failed-tool display data between persistence
  and event.
- Changed files and final commit are recorded in `task-3-report.md` after commit.
- Task 3 fix-round-1 re-review: Spec compliance FAIL; one Important blocker remains.
- Blocking finding: `ConversationTaskContextCrud.create` swallows every `IntegrityError` for ToolMessage
  rows as duplicate, including sequence conflicts, foreign-key failures and other schema errors. Only
  the intended `(task_id, run_id, tool_call_id)` uniqueness violation may return duplicate; all other
  database failures must roll back and propagate.
- Additional review findings: normal completed/failed/cancelled Run projector failures are not uniformly
  isolated after commit; raw observation error text can still enter Transport metadata. Treat the former
  as required post-commit degradation and the latter as controlled metadata hardening.
- Task 3 fix round 2/5 dispatched to Locke; independent re-review required.
- Fix round 2 was interrupted by the implementer usage limit; the controller completed the remaining
  TDD fixes locally. RED covered non-target sequence IntegrityError and external-session event deferral;
  GREEN focused Task 3/fact-model suite `64 passed`.
- Controller commits: `b0b3a53` (duplicate DB failure semantics and terminal event degradation),
  `a4a9d4c` (external-session initialization event ownership and safe command publisher).
- Final independent review: Spec compliance PASS; Task quality PASS; Task 3 can complete. Focused
  Task 3/fact-model tests `63 passed`, related `43 passed` with one known legacy snapshot-recovery
  failure, compileall and diff check passed.

## Task 4

- Brief: `.superpowers/sdd/conversation-state-rebuild-from-context-plan/task-4-brief.md`.
- Implementer: Jason (`01a093e0-4430-7021-b9df-480c8d785515`).
- Implementation commits: `2d337a7`, report `51ef989`, verification correction `dd7256c`.
- Implementer evidence: core regression `152 passed`; full backend `356 passed, 1 skipped`, with
  only the environment-missing `terminal-worker.exe` failure; Ruff/compileall/focused MyPy passed.
- Claimed scope: removed conversation snapshot model/CRUD/service and switched API, SSE,
  attach/resume, fork/edit/delete/recovery cold reads to the three-model state service; preserved live
  projector/SSE and explicit checkpoint continuation.
- Status: implementation complete, independent review pending.
- Fix round 1 implementation commits: `ee66a21`; report/verification updates are recorded in the
  Task 4 report and latest HEAD `0dd66ac`.
- Task 4 fix evidence: implementer reports focused/core `152+` passing, full backend `356 passed,
  1 skipped` with only missing `terminal-worker.exe`; Ruff/compileall/focused MyPy passed.
- Task 4 fix round 1 independent review pending; Task 5 remains blocked.
- Task 4 fix-round-1 final review: Spec compliance PASS; Task quality PASS; no Important/Minor
  findings. Focused/related suite `156 passed`; full backend `364 passed, 1 skipped, 1 failed`, sole
  failure missing Windows `terminal-worker.exe`; Ruff/compileall passed. Task 4 complete.

## Task 5

- Brief: `.superpowers/sdd/conversation-state-rebuild-from-context-plan/task-5-brief.md`.
- Status: ready to dispatch after Task 4 passed independent review.
- Task 4 review verdict: Spec compliance FAIL; Task quality partial; Task 4 blocked.
- Important findings: `_merge_canonical_facts` lets active in-memory messages hide committed context
  after projector failure; `claim_or_resume_run` publishes directly and can orphan a committed Run as
  running when projection fails; forked Run clone still copies source `checkpoint_thread_id` despite the
  plan requiring a fresh checkpoint identity.
- Minor findings: malformed metadata from SQLite can escape as an unclassified exception before the
  structured rebuild error boundary; Task 4 tests are overly mocked and deletion coverage does not
  verify canonical rows/side-effect boundaries.
- Ruling: canonical facts must win over stale memory while preserving only uncommitted active deltas;
  all post-commit claim/resume events must be failure-isolated; fork clone must never share checkpoint
  identity. Add real-storage/lifecycle tests and explicit malformed-data coverage.
- Task 4 fix round 1/5 dispatched to the original implementer; independent re-review required.
- Implementer: Halley (`01a09411-125f-70c2-9c13-033238752b92`).
- Acceptance commit: `7fc5b166cda6ce2b4863237c2d7be4d8fe8ed6ea`.
- Evidence: Task 5 acceptance `9 passed`, post-commit/tool regression `20 passed`, full backend
  `377 passed, 1 skipped` with one environment-only missing `terminal-worker.exe` failure; Ruff,
  compileall, forbidden-reference grep and diff check passed. TDD fixes covered nullable tool output,
  edit/restart external transaction flush and stale generation protection.
- Implementer did not perform the required independent subagent review; final acceptance review pending.
- Independent final review rejected the candidate despite all six blockers being closed: required
  persistence-boundary logs were absent, and `tool_contract.md` had an EOF whitespace issue.
- Controller fix: added `context_message_persisted`, `tool_observation_persisted`, and
  `run_status_persisted` structured logs; removed the EOF whitespace. Focused regression remains
  required before the final review is repeated.
- Final closure: commit `51732e1` added the persistence-boundary logging/Tool cancellation contract;
  `dec36e0f` corrected the new/edit Run ids in those logs; `78073fea` made orphan-recovery logging
  defensive for lightweight test doubles. Targeted acceptance/compile/Ruff checks passed.
- Final independent subagent review on `78073fea`: Spec compliance PASS, Task quality PASS, no
  Important or Minor findings. Focused acceptance `89 passed`; full backend `388 passed, 1 skipped`
  with one environment-only failure because `apps/terminal-worker/target/debug/terminal-worker.exe`
  is absent (`WinError 2`).
- Task 5 and the conversation-state rebuild plan are complete.
