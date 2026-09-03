# Mypy baseline

`apps/backend/pyproject.toml` keeps strict mypy enabled while applying the
documented legacy-module overrides. The repository still contains pre-existing
errors in legacy storage, model, provider, delegation, and tool modules.

The accepted type-checking boundary is:

- `uv run mypy app` is an informational full-repository baseline and must not be
  used as evidence that a change is type-safe while the baseline is non-zero.
- New `app/` files and files changed in a feature are checked by the strict
  `apps/backend/mypy.strict.ini` hook without legacy exemptions.
- A feature is not allowed to add a new baseline error. Existing baseline
  entries must be removed as their owning module is touched.

The current baseline is tracked by the CI/pre-commit output rather than copied
into this document, so diagnostics always point at the current source lines.
The conversation Transport and executor changes in this refactor pass Ruff,
focused pytest coverage, and the strict checks applicable to the changed API,
runtime, and test signatures.
