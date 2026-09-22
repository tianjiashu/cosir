# Task 3 — Child Agent UI rendering and acceptance

## Scope

Implemented frontend-only Child Agent rendering and acceptance coverage. No backend,
Tauri, SQLite, runtime, or Git commit changes were made.

## Implemented

- Added a typed Child Agent display projection for delegation and `child-agent-wait-result`.
- Updated the delegation row to use the compact terminal visual language, show stable
  task/run references, role, lifecycle status, controlled status hints, and completed
  final output without rendering prompt/raw exception-shaped fields.
- Preserved Workbench Agent opening through the existing store/context and `cancelRun`
  backend boundary for Stop. The existing Agent surface remains readonly and has no
  second composer; tab close remains a frontend tab action.
- Routed `child-agent-wait-result` through the generic `display_data.kind` route and
  render it in `DetailsTool`; unknown and malformed payloads use the existing fallback.
- Tightened snapshot validation for delegation and child-wait identifiers, statuses,
  result shapes, interruption values, and allowlisted display fields.

## TDD evidence

RED was observed before production implementation:

```text
npm exec vitest run tests/unit/delegation-tool-row.test.tsx tests/unit/tool-part.test.ts tests/unit/details-tool.test.tsx tests/unit/snapshot-validation.test.ts
8 failed, 27 passed
```

The failures covered missing child session projection, missing wait-kind routing,
malformed fallback behavior, and missing snapshot payload validation.

## Verification

Passed:

```text
npm exec vitest run tests/unit/readonly-thread.test.tsx tests/unit/workbench-store.test.ts tests/unit/assistant-attach-controller.test.ts tests/unit/delegation-tool-row.test.tsx tests/unit/tool-part.test.ts tests/unit/details-tool.test.tsx tests/unit/snapshot-validation.test.ts
7 files passed, 45 tests passed

npm run test:unit
46 files passed, 225 tests passed

npm run build
tsc --noEmit passed; vite build passed

npm exec eslint components/assistant-ui/tools/child-agent-display.ts components/assistant-ui/tools/delegation-tool-row.tsx components/assistant-ui/tools/details-tool.tsx components/assistant-ui/tools/tool-part.tsx lib/assistant/snapshot-validation.ts tests/unit/delegation-tool-row.test.tsx tests/unit/details-tool.test.tsx tests/unit/snapshot-validation.test.ts tests/unit/tool-part.test.ts tests/unit/workbench-store.test.ts tests/unit/readonly-thread.test.tsx
passed

git diff --check
passed
```

## E2E

Command:

```text
npm run test:e2e -- tests/e2e/workbench-subagent.spec.ts
```

The first attempt was blocked by the sandbox refusing the Vite listener:
`listen EPERM: operation not permitted 127.0.0.1:4173`.

After allowing the required local test-service permission, the service started, but
both existing Workbench/subagent tests were blocked before execution because the
Playwright Chromium executable is not installed:
`Executable doesn't exist at .../ms-playwright/.../chrome-headless-shell`.
No browser download was performed.

Existing Vitest coverage still verifies attach deduplication/retry and the new
readonly/close Workbench behavior; the existing E2E spec remains the browser-level
acceptance path when Chromium is available.

