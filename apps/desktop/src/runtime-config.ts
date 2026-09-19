import { invoke } from "@tauri-apps/api/core";
import { frontendLog } from "@/lib/logging/frontend-log";

declare global {
  interface Window {
    __COSIR_RUNTIME_CONFIG__?: { backendBaseUrl?: string };
    __TAURI_INTERNALS__?: unknown;
    __TAURI__?: unknown;
  }
}

const runtimeListeners = new Set<() => void>();
const statusListeners = new Set<() => void>();
const STARTUP_TIMEOUT_MS = 95_000;
const RUNTIME_MONITOR_INTERVAL_MS = 2_000;
const UNAVAILABLE_BACKEND_BASE_URL = "http://127.0.0.1:0";
const BACKEND_UNAVAILABLE_MESSAGE = "本地 Agent 后端状态不可用，请点击重试";

export type BackendRuntimeSnapshot = {
  backendBaseUrl: string;
  generation: number;
  available: boolean;
};

export type BackendStatus =
  | { state: "starting" | "ready" | "stopped" }
  | { state: "failed"; message: string };

export type BackendStatusSnapshot = BackendStatus | null;

export type BackendRuntimeConfig = {
  backendBaseUrl: string;
  generation: number;
  status: BackendStatus;
};

let backendRuntimeGeneration = 0;
let backendRuntimeSnapshot: BackendRuntimeSnapshot | null = null;
let acceptedBackendBaseUrl: string | null = null;
let acceptedBackendGeneration = 0;
let backendStatusSnapshot: BackendStatusSnapshot = null;
let operationEpoch = 0;
let initializeInFlight: Promise<string> | null = null;
let refreshInFlight: Promise<void> | null = null;
let restartInFlight: Promise<BackendRuntimeConfig> | null = null;
let monitorDisposer: (() => void) | null = null;
let backendRuntimeUnavailable = false;

class BackendRuntimeOperationSupersededError extends Error {
  constructor() {
    super("后端生命周期操作已被更新的操作取代");
    this.name = "BackendRuntimeOperationSupersededError";
  }
}

function isTauriRuntime(): boolean {
  return Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
}

function beginOperation(): number {
  operationEpoch += 1;
  return operationEpoch;
}

function assertCurrentOperation(epoch: number): void {
  if (epoch !== operationEpoch) throw new BackendRuntimeOperationSupersededError();
}

function isSuperseded(error: unknown): boolean {
  return error instanceof BackendRuntimeOperationSupersededError;
}

function normalizeBackendBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/$/, "");
}

function sameBackendStatus(left: BackendStatusSnapshot, right: BackendStatusSnapshot): boolean {
  if (left === right) return true;
  if (!left || !right || left.state !== right.state) return false;
  return left.state !== "failed" || right.state !== "failed" || left.message === right.message;
}

function publishBackendStatus(status: BackendStatusSnapshot): void {
  if (sameBackendStatus(backendStatusSnapshot, status)) return;
  backendStatusSnapshot = status;
  for (const listener of statusListeners) listener();
}

function notifyRuntimeListeners(): void {
  for (const listener of runtimeListeners) listener();
}

function readSupervisorStatus(): Promise<BackendStatus> {
  return invoke<BackendStatus>("backend_status");
}

function readSupervisorConfig(): Promise<BackendRuntimeConfig> {
  return invoke<BackendRuntimeConfig>("backend_runtime_config");
}

function applySupervisorConfig(config: BackendRuntimeConfig): { accepted: boolean; runtimeChanged: boolean } {
  const normalizedBaseUrl = normalizeBackendBaseUrl(config.backendBaseUrl);

  if (acceptedBackendBaseUrl !== null && config.generation < acceptedBackendGeneration) {
    // Polling responses can arrive out of order. A stale supervisor snapshot must not
    // move the frontend back to an older backend instance.
    return { accepted: false, runtimeChanged: false };
  }

  if (
    acceptedBackendBaseUrl !== null
    && config.generation === acceptedBackendGeneration
    && acceptedBackendBaseUrl !== normalizedBaseUrl
  ) {
    void frontendLog(
      "WARNING",
      "backend_runtime_config_conflict",
      "拒绝相同 generation 的不同后端地址",
      { data: { generation: config.generation } },
    );
    return { accepted: false, runtimeChanged: false };
  }

  const runtimeChanged =
    backendRuntimeSnapshot?.backendBaseUrl !== normalizedBaseUrl
    || backendRuntimeSnapshot?.generation !== config.generation
    || backendRuntimeSnapshot?.available !== true;

  backendRuntimeGeneration = config.generation;
  acceptedBackendBaseUrl = normalizedBaseUrl;
  acceptedBackendGeneration = config.generation;
  backendRuntimeUnavailable = false;
  window.__COSIR_RUNTIME_CONFIG__ = { backendBaseUrl: normalizedBaseUrl };
  backendRuntimeSnapshot = {
    backendBaseUrl: normalizedBaseUrl,
    generation: backendRuntimeGeneration,
    available: true,
  };
  return { accepted: true, runtimeChanged };
}

function commitSupervisorConfig(config: BackendRuntimeConfig): boolean {
  const result = applySupervisorConfig(config);
  if (!result.accepted) return false;
  // Both projections are updated before either listener set is notified. Consumers
  // therefore never observe a new runtime generation with the previous status.
  publishBackendStatus(config.status);
  if (result.runtimeChanged) notifyRuntimeListeners();
  return true;
}

function invalidateBackendRuntimeProjection(): boolean {
  const runtimeChanged =
    !backendRuntimeUnavailable
    || backendRuntimeSnapshot?.backendBaseUrl !== UNAVAILABLE_BACKEND_BASE_URL;
  backendRuntimeUnavailable = true;
  window.__COSIR_RUNTIME_CONFIG__ = { backendBaseUrl: UNAVAILABLE_BACKEND_BASE_URL };
  backendRuntimeSnapshot = {
    backendBaseUrl: UNAVAILABLE_BACKEND_BASE_URL,
    generation: backendRuntimeGeneration,
    available: false,
  };
  return runtimeChanged;
}

function publishUnavailableStatus(status: BackendStatusSnapshot): void {
  const runtimeChanged = invalidateBackendRuntimeProjection();
  publishBackendStatus(status);
  if (runtimeChanged) notifyRuntimeListeners();
}

function getRestartFailureMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return "本地 Agent 后端重启失败，请点击重试";
}

function waitForNextStartupPoll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 250));
}

async function initializeBackendRuntimeInternal(epoch: number): Promise<string> {
  const configured = getBackendBaseUrl();
  if (configured && !isTauriRuntime()) {
    publishBackendStatus({ state: "ready" });
    return normalizeBackendBaseUrl(configured);
  }

  if (!isTauriRuntime()) {
    const developmentUrl = import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";
    const config: BackendRuntimeConfig = {
      backendBaseUrl: developmentUrl,
      generation: backendRuntimeGeneration,
      status: { state: "ready" },
    };
    assertCurrentOperation(epoch);
    commitSupervisorConfig(config);
    return normalizeBackendBaseUrl(developmentUrl);
  }

  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let lastError = "本地 Agent 后端尚未就绪";
  while (Date.now() < deadline) {
    assertCurrentOperation(epoch);

    let status: BackendStatus;
    try {
      status = await readSupervisorStatus();
      assertCurrentOperation(epoch);
      if (status.state === "ready") publishBackendStatus(status);
      else publishUnavailableStatus(status);
    } catch (error) {
      if (isSuperseded(error)) throw error;
      if (error instanceof Error && error.message) lastError = error.message;
      await waitForNextStartupPoll();
      continue;
    }

    const statusError = getBackendStatusError(status);
    if (statusError) throw statusError;

    if (status.state === "ready") {
      try {
        const config = await readSupervisorConfig();
        assertCurrentOperation(epoch);
        const configError = getBackendStatusError(config.status);
        if (configError) throw configError;
        if (!commitSupervisorConfig(config)) {
          throw new Error("收到无法应用的后端运行时配置");
        }
        return normalizeBackendBaseUrl(config.backendBaseUrl);
    } catch (error) {
      if (isSuperseded(error)) throw error;
      if (error instanceof Error && error.message) lastError = error.message;
      if (epoch === operationEpoch) {
        publishUnavailableStatus({ state: "failed", message: lastError });
      }
    }
    }

    await waitForNextStartupPoll();
  }
  throw new Error(lastError);
}

/** 返回当前后端实例快照，供 React runtime 外部 store 订阅使用。 */
export function getBackendRuntimeSnapshot(): BackendRuntimeSnapshot {
  const backendBaseUrl = resolveBackendBaseUrl();
  if (backendRuntimeSnapshot?.backendBaseUrl !== backendBaseUrl) {
    backendRuntimeSnapshot = {
      backendBaseUrl,
      generation: backendRuntimeGeneration,
      available: !backendRuntimeUnavailable,
    };
  }
  return backendRuntimeSnapshot;
}

/** 返回当前后端生命周期状态快照，供启动页和状态 Banner 订阅使用。 */
export function getBackendStatusSnapshot(): BackendStatusSnapshot {
  return backendStatusSnapshot;
}

/** 返回当前后端地址。 */
export function getBackendBaseUrlSnapshot(): string {
  return getBackendRuntimeSnapshot().backendBaseUrl;
}

/** 订阅 Tauri supervisor 切换后端进程或端口的通知。 */
export function subscribeBackendRuntime(listener: () => void): () => void {
  runtimeListeners.add(listener);
  return () => runtimeListeners.delete(listener);
}

/** 订阅 Tauri supervisor 的状态投影，不会触发 Assistant runtime listener。 */
export function subscribeBackendStatus(listener: () => void): () => void {
  statusListeners.add(listener);
  return () => statusListeners.delete(listener);
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

/** 启动门禁只调用这一处 supervisor 查询链；旧启动响应不会再发布状态或配置。 */
export function initializeBackendRuntime(): Promise<string> {
  if (initializeInFlight) return initializeInFlight;

  const epoch = beginOperation();
  const promise = initializeBackendRuntimeInternal(epoch).finally(() => {
    if (initializeInFlight === promise) initializeInFlight = null;
  });
  initializeInFlight = promise;
  return promise;
}

async function refreshBackendRuntimeInternal(epoch: number): Promise<void> {
  if (!isTauriRuntime()) return;

  let status: BackendStatus;
  try {
    status = await readSupervisorStatus();
    assertCurrentOperation(epoch);
  } catch (error) {
    if (isSuperseded(error)) throw error;
    if (epoch === operationEpoch) {
      publishUnavailableStatus({ state: "failed", message: BACKEND_UNAVAILABLE_MESSAGE });
    }
    return;
  }

  if (status.state !== "ready") {
    publishUnavailableStatus(status);
    return;
  }

  try {
    const config = await readSupervisorConfig();
    assertCurrentOperation(epoch);
    if (getBackendStatusError(config.status)) {
      publishUnavailableStatus(config.status);
      return;
    }
    commitSupervisorConfig(config);
  } catch (error) {
    if (isSuperseded(error)) throw error;
    if (epoch === operationEpoch) {
      publishUnavailableStatus({ state: "failed", message: BACKEND_UNAVAILABLE_MESSAGE });
    }
  }
}

/** 运行期状态同步采用单飞请求，避免 Banner 和 monitor 各自轮询。 */
export function refreshBackendRuntime(): Promise<void> {
  if (restartInFlight) return restartInFlight.then(() => undefined, () => undefined);
  if (initializeInFlight) return initializeInFlight.then(() => undefined, () => undefined);
  if (refreshInFlight) return refreshInFlight;

  const epoch = beginOperation();
  const promise = refreshBackendRuntimeInternal(epoch)
    .catch((error: unknown) => {
      if (isSuperseded(error)) return;
      throw error;
    })
    .finally(() => {
      if (refreshInFlight === promise) refreshInFlight = null;
    });
  refreshInFlight = promise;
  return promise;
}

/** 启动唯一前端 monitor；清理会使未完成的 refresh 结果失效。 */
export function startBackendRuntimeMonitor(): () => void {
  if (!isTauriRuntime()) return () => undefined;
  if (monitorDisposer) return monitorDisposer;

  let disposed = false;
  const poll = () => {
    if (!disposed) void refreshBackendRuntime().catch(() => undefined);
  };
  const timer = window.setInterval(poll, RUNTIME_MONITOR_INTERVAL_MS);
  const disposer = () => {
    if (disposed) return;
    disposed = true;
    window.clearInterval(timer);
    if (monitorDisposer === disposer) monitorDisposer = null;
    beginOperation();
  };
  monitorDisposer = disposer;
  void refreshBackendRuntime().catch(() => undefined);
  return disposer;
}

/** 运行期唯一前端重启入口；并发 retry 复用同一个 supervisor command。 */
export function restartBackendRuntime(): Promise<BackendRuntimeConfig> {
  if (restartInFlight) return restartInFlight;

  const epoch = beginOperation();
  publishUnavailableStatus({ state: "starting" });
  const promise = invoke<BackendRuntimeConfig>("restart_backend")
    .then((config) => {
      assertCurrentOperation(epoch);
      const configError = getBackendStatusError(config.status);
      if (configError) throw configError;
      if (!commitSupervisorConfig(config)) {
        throw new Error("收到无法应用的后端运行时配置");
      }
      return config;
    })
    .catch((error: unknown) => {
      if (!isSuperseded(error) && epoch === operationEpoch) {
        publishUnavailableStatus({ state: "failed", message: getRestartFailureMessage(error) });
      }
      throw error;
    })
    .finally(() => {
      if (restartInFlight === promise) restartInFlight = null;
    });
  restartInFlight = promise;
  return promise;
}

function resolveBackendBaseUrl(): string {
  if (backendRuntimeUnavailable) return UNAVAILABLE_BACKEND_BASE_URL;
  return getBackendBaseUrl() ?? import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";
}

function getBackendBaseUrl(): string | null {
  return window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl ?? null;
}
