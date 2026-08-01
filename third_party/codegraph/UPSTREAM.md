# CodeGraph Vendor Source

Upstream: `/Users/woaigugu/Documents/codegraph`

Vendored version: `1.4.1`

License: MIT, see `LICENSE`.

Purpose: embed CodeGraph as a managed local code-intelligence kernel for this
coding-agent project. Local integration code should be added in a narrow adapter
layer first, such as `src/agent-kernel/`, instead of scattering project-specific
changes across upstream core modules.

Excluded from vendoring:

- `.git/`
- `.codegraph/`
- `node_modules/`
- `dist/`
- nested package `node_modules/`
