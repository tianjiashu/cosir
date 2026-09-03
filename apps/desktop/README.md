# Cosir desktop

Cosir is a local Tauri desktop application. The UI is built as static React/Vite
assets and runs in the Tauri WebView. The Tauri Rust host starts the local FastAPI
backend, waits for `/health`, and exposes the actual loopback URL to the UI through
the `backend_runtime_config` command.

## Development

From this directory:

```text
npm install
npm run tauri:dev
```

The Tauri host starts the Vite development server, then starts the Python backend.
For browser-only UI work, set `VITE_BACKEND_URL` and run `npm run dev`.

## Build

```text
npm run build
```

The output is written to `dist/` and contains no Node.js server. The production
desktop process boundary is Tauri → Python/FastAPI; application data and canonical
conversation state remain owned by the backend storage layer.
