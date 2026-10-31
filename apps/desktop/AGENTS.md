# Desktop application notes

The desktop UI is a static React/Vite application hosted by Electron. The main process
(`electron/main/`) shows the window and starts the local FastAPI process in the background;
the frontend boot gate presents readiness independently of window visibility. The
renderer reads the backend URL only from the host's backend snapshot
(`window.cosirHost.backend`, via `lib/desktop-host/`) and connects to it directly;
there is no frontend server, proxy route, or client-side tool execution.
