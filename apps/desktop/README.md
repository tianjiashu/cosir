# Cosir desktop

Cosir is a local Electron desktop application. The UI is built as static React/Vite
assets and runs in the Electron renderer. The Electron main process (`electron/main/`)
starts the local FastAPI backend, waits for its boot state and `/health`, and pushes
the actual loopback URL to the UI through `window.cosirHost.backend`.

## Development

From this directory:

```text
npm install
npm run electron:dev
```

The development entry builds the root Cargo workspace's Terminal Worker first and
injects its absolute path into the host, then starts Vite and Electron; the host
starts the Python backend. Main and preload are rebuilt with esbuild on change and
Electron restarts (the backend restarts with it). Missing worker artifacts fail
startup instead of silently disabling the terminal capability.
For browser-only UI work, set `VITE_BACKEND_URL` and run `npm run dev`.

Set `CODING_AGENT_DATA_DIR` to run the desktop app against an isolated data root
instead of the user's home directory.

## Build

```text
npm run build
```

Writes the renderer to the repository-level `target/frontend/` and the main and
preload bundles (plus icons) to `target/electron/`; `package.json` `main` points at
`target/electron/main.cjs`. The production desktop process boundary is
Electron → Python/FastAPI; application data and canonical conversation state remain
owned by the backend storage layer.

`npm run build:bundle` additionally builds the platform-specific Rust Terminal
Worker in release mode (staged at `target/resources/terminal-worker/`) and the
Python backend (staged at `target/resources/backend/`). Installer packaging of
these resources is not wired up yet. All Rust and staging scripts use the
repository-level `target/` as the single Cargo target directory, even when the
caller exports `CARGO_TARGET_DIR`.
`npm run test:terminal-worker-bundle` verifies the staged resource by starting it
through the stdio protocol (`COSIR_TERMINAL_RESOURCE_DIR` points it at another
resource directory).

## Tests

```text
npm run test:unit         # Vitest, including the main-process supervisor rules
npm run test:e2e          # Playwright against Vite and the in-memory test service
npm run test:e2e:desktop  # Playwright driving the real Electron host and backend
```

`test:e2e` starts its own Vite (4173), test service (8000) and output directory
(`test-results/slot-0`), and fails if a port is already taken. To run several
suites at once, give each a different slot: `COSIR_E2E_SLOT=1 npm run test:e2e`
shifts every port by 1 and uses `test-results/slot-1` (see `tests/e2e/e2e-env.ts`).
