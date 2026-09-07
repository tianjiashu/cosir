import { invoke } from "@tauri-apps/api/core";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: { backendBaseUrl?: string };
    __TAURI_INTERNALS__?: unknown;
    __TAURI__?: unknown;
  }
}

const runtimeConfigListeners = new Set<() => void>();
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

/** 返回当前后端地址，供 React 外部 store 订阅使用。 */
export function getBackendBaseUrlSnapshot(): string {
  return getBackendBaseUrl() ?? import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";
}

/** 订阅 Tauri supervisor 切换后端进程或端口的通知。 */
export function subscribeBackendRuntime(listener: () => void): () => void {
  runtimeConfigListeners.add(listener);
  return () => runtimeConfigListeners.delete(listener);
}

/**
 * 将 Tauri supervisor 的终态转换为启动错误。
 *
 * supervisor 返回的 message 是后端启动事实的安全摘要；前端只在状态明确为
 * failed/stopped 时展示它，不根据网络超时或其它本地猜测伪造后端错误。
 */
export function getBackendStatusError(status: BackendStatus): Error | null {
  if (status.state === "failed") {
    const message = status.message.trim() || "本地 Agent 后端启动失败，请点击重试";
    return new Error(message);
  }
  if (status.state === "stopped") {
    return new Error("本地 Agent 后端未运行，请点击重试");
  }
  return null;
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
    let status: BackendStatus;
    try {
      status = await invoke<BackendStatus>("backend_status");
    } catch (error) {
      if (error instanceof Error && error.message) lastError = error.message;
      await new Promise((resolve) => window.setTimeout(resolve, 250));
      continue;
    }

    const statusError = getBackendStatusError(status);
    if (statusError) throw statusError;

    if (status.state === "ready") {
      try {
        const config = await invoke<BackendRuntimeConfig>("backend_runtime_config");
        const configError = getBackendStatusError(config.status);
        if (configError) throw configError;
        setBackendBaseUrl(config.backendBaseUrl);
        return config.backendBaseUrl.replace(/\/$/, "");
      } catch (error) {
        if (error instanceof Error && error.message) lastError = error.message;
      }
    }

    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  throw new Error(lastError);
}

export function setBackendBaseUrl(baseUrl: string): void {
  window.__COSIR_RUNTIME_CONFIG__ = { backendBaseUrl: baseUrl.replace(/\/$/, "") };
  for (const listener of runtimeConfigListeners) listener();
}

export async function restartBackendRuntime(): Promise<BackendRuntimeConfig> {
  const config = await invoke<BackendRuntimeConfig>("restart_backend");
  setBackendBaseUrl(config.backendBaseUrl);
  return config;
}
