---
name: dead-code-detection
description: Run and review this repository's Knip and Vulture dead-code scans. Use when installing, operating, tuning, or interpreting these scanners; do not use for unrelated linting or code cleanup.
---

# Dead-code detection in Cosir

Use this skill for the repository's frontend and backend dead-code scanners. The tools have separate language and runtime boundaries, and their findings require source-level review before cleanup.

## Run

From the repository root, run both tools:

```bash
node skills/dead-code-detection/scripts/check-dead-code.mjs
```

Install their locked dependencies when needed:

```bash
npm ci --prefix apps/desktop
uv sync --project apps/backend --group dev
```

Knip checks frontend JavaScript and TypeScript under `apps/desktop`. Its npm script and dependency live in that app's `package.json`; its dependency versions are locked in `apps/desktop/package-lock.json`.

Vulture checks Python production code under `apps/backend/app/`, with `min_confidence = 80` and size sorting configured in `apps/backend/pyproject.toml`. It is a backend development dependency locked by `apps/backend/uv.lock`. The current scan does not include `apps/backend/tests/` or Rust/Tauri code.

The runner executes both scanners even if one reports findings, then exits nonzero if either scanner fails or reports issues. Treat a nonzero result as a review queue, not as permission to delete every reported item.

## Review findings

Check each reported definition and its real callers before changing code. In this app, static import analysis may not see component registries, routes, dynamic imports, Tauri configuration, or test commands. Python analysis can miss or misclassify framework callbacks, reflection, and fixture injection.

For confirmed implicit references, use a narrow tool-supported configuration or whitelist. Avoid broad ignores that hide future findings. After removing confirmed dead code, rerun the scan because cleanup can expose additional unused items.

## Current baseline

The reviewed Knip baseline currently has no unused files, dependencies, dev dependencies, exports, or exported types. Confirmed unused frontend code and the unused `@codemirror/search` and `diff` dependencies were removed. The deprecated `components/assistant-ui/tools/unknown-tool.tsx` wrapper was also removed.

Knip still reports unlisted direct imports (`@assistant-ui/core`, `assistant-stream/utils`, `streamdown`, `react-markdown`, `@eslint/js`, and `mdast`) and one duplicate export pair: `referenceIdentity` aliases `encodeReference` and is used by the composer as a key. These are dependency declaration and naming findings, not dead-code candidates. Configuration hints about redundant entry patterns are informational.

The configured Vulture scan reports no findings in `apps/backend/app/` at 80% confidence. It does not scan `apps/backend/tests/` or Rust/Tauri code.
