import { invoke } from "@tauri-apps/api/core";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: { backendBaseUrl?: string };
    __TAURI_INTERNALS__?: unknown;
    __TAURI__?: unknown;
  }
}

const STARTUP_TIMEOUT_MS = 95_000;

export type BackendRuntimeConfig = {
  backendBaseUrl: string;
  status: BackendStatus;
};

export type BackendStatus =
  | { state: "starting" | "ready" | "stopped" }
  | { state: "failed"; message: string };

export function getBackendBaseUrl(): string | null {
  return window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl ?? null;
}

export async function initializeBackendRuntime(): Promise<string> {
  const configured = getBackendBaseUrl();
  if (configured) return configured.replace(/\/$/, "");

  if (!window.__TAURI_INTERNALS__ && !window.__TAURI__) {
    const developmentUrl = import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";
    window.__COSIR_RUNTIME_CONFIG__ = { backendBaseUrl: developmentUrl };
    return developmentUrl.replace(/\/$/, "");
  }

  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let lastError = "本地 Agent 后端尚未就绪";
  while (Date.now() < deadline) {
    try {
      const config = await invoke<BackendRuntimeConfig>("backend_runtime_config");
      setBackendBaseUrl(config.backendBaseUrl);
      return config.backendBaseUrl.replace(/\/$/, "");
    } catch (error) {
      if (error instanceof Error && error.message) lastError = error.message;
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
  }
  throw new Error(lastError);
}

export function setBackendBaseUrl(baseUrl: string): void {
  window.__COSIR_RUNTIME_CONFIG__ = { backendBaseUrl: baseUrl.replace(/\/$/, "") };
}

export async function restartBackendRuntime(): Promise<BackendRuntimeConfig> {
  const config = await invoke<BackendRuntimeConfig>("restart_backend");
  setBackendBaseUrl(config.backendBaseUrl);
  return config;
}
