# Desktop application notes

The desktop UI is a static React/Vite application hosted by Tauri. Tauri starts and
supervises the local FastAPI process before the main UI becomes interactive. The
browser-side API client connects directly to the runtime backend URL supplied by a
Tauri command; there is no frontend server, proxy route, or client-side tool execution.
