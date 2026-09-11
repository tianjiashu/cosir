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
- Implementer: Codex，直接在共享 workspace 执行；无可用子 Agent 工具，因此未创建子 thread。
- TDD RED：Task 3 canonical write-path focused tests first，首次 `5 failed`，扩展覆盖后
  `9 failed`，fresh Run 重载用例单独 `1 failed`。
- TDD GREEN：Task 3 focused `10 passed`；相关 context/tool/usage 回归 `29 passed`，
  tool repair/observation 回归 `8 passed`。
- 静态检查：Ruff 与 `compileall` 通过；聚焦 MyPy 触达两个未修改的既有 event TypedDict
  类型错误。
- 完整后端回归：`336 passed, 1 skipped, 7 failed`；失败为已知 snapshot cleanup/read
  边界与缺少 terminal-worker sidecar，详见 `task-3-report.md`。
- Status: DONE_WITH_CONCERNS；未触碰 Task 4 snapshot removal/read-path switching。
