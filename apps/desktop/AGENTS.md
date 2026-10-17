# Desktop application notes

The desktop UI is a static React/Vite application hosted by Tauri. Tauri shows the window and starts the local FastAPI process in the background;
the frontend boot gate presents readiness independently of window visibility. The
browser-side API client connects directly to the runtime backend URL supplied by a
Tauri command; there is no frontend server, proxy route, or client-side tool execution.
