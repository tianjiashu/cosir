import type { BackendSnapshot, BackendStatus } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 渲染进程里后端地址与状态的唯一投影。
 *
 * 桌面宿主中，事实源是宿主 supervisor：启动时取一次快照，之后只订阅它的推送，不轮询。快照带
 * `generation`，比当前已接受的更旧的一律丢弃，旧后端实例的状态不会覆盖新实例。不在宿主中（浏览器开发、
 * E2E）时地址取 `VITE_BACKEND_URL`，状态恒为 ready。
 *
 * 两类订阅分开：`subscribeBackendRuntime` 只在地址或 generation 变化时通知（Assistant 会话据此重连），
 * `subscribeBackendStatus` 只在状态变化时通知（启动页与状态横幅）。两份投影先同时更新再通知，
 * 订阅方不会看到「新 generation + 旧状态」。
 */

export type BackendRuntimeSnapshot = {
  backendBaseUrl: string;
  generation: number;
  available: boolean;
};

type BackendStatusSnapshot = BackendStatus | null;

const UNAVAILABLE_BACKEND_BASE_URL = "http://127.0.0.1:0";

const runtimeListeners = new Set<() => void>();
const statusListeners = new Set<() => void>();
const hostSnapshotListeners = new Set<(snapshot: BackendSnapshot) => void>();

let runtimeSnapshot: BackendRuntimeSnapshot = {
  backendBaseUrl: desktopHost ? UNAVAILABLE_BACKEND_BASE_URL : developmentBackendBaseUrl(),
  generation: 0,
  available: !desktopHost,
};
let statusSnapshot: BackendStatusSnapshot = null;
let acceptedSnapshot: BackendSnapshot | null = null;
let hostSubscription: (() => void) | null = null;
let restartInFlight: Promise<void> | null = null;

function normalizeBackendBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/$/, "");
}

/** 浏览器开发 / 测试（不在桌面宿主中）时的后端地址；桌面地址只来自宿主快照。 */
function developmentBackendBaseUrl(): string {
  return normalizeBackendBaseUrl(import.meta.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000");
}

function sameStatus(left: BackendStatusSnapshot, right: BackendStatusSnapshot): boolean {
  if (left === right) return true;
  if (!left || !right || left.state !== right.state) return false;
  return left.state !== "failed" || right.state !== "failed" || left.message === right.message;
}

/** 应用一份宿主快照；过期快照被丢弃。 */
function applyHostSnapshot(snapshot: BackendSnapshot): void {
  if (acceptedSnapshot && snapshot.generation < acceptedSnapshot.generation) return;
  acceptedSnapshot = snapshot;
  const ready = snapshot.status.state === "ready" && snapshot.backendBaseUrl !== null;
  const nextRuntime: BackendRuntimeSnapshot = {
    // 宿主给出的地址由 supervisor 拼成 `http://127.0.0.1:<port>`，不带尾斜杠，不需要规范化。
    backendBaseUrl: ready ? snapshot.backendBaseUrl! : UNAVAILABLE_BACKEND_BASE_URL,
    generation: snapshot.generation,
    available: ready,
  };
  const runtimeChanged = nextRuntime.backendBaseUrl !== runtimeSnapshot.backendBaseUrl
    || nextRuntime.generation !== runtimeSnapshot.generation
    || nextRuntime.available !== runtimeSnapshot.available;
  const statusChanged = !sameStatus(statusSnapshot, snapshot.status);
  if (runtimeChanged) runtimeSnapshot = nextRuntime;
  if (statusChanged) statusSnapshot = snapshot.status;
  if (statusChanged) for (const listener of statusListeners) listener();
  if (runtimeChanged) for (const listener of runtimeListeners) listener();
  for (const listener of hostSnapshotListeners) listener(snapshot);
}

/** 订阅宿主推送（整个页面生命周期只订阅一次），并取一次当前快照补齐订阅之前的状态。 */
function ensureHostSubscription(host: NonNullable<typeof desktopHost>): void {
  if (hostSubscription) return;
  hostSubscription = host.backend.onChange((snapshot) => {
    applyHostSnapshot(snapshot);
  });
  void host.backend.snapshot().then(applyHostSnapshot, (error: unknown) => {
    void frontendLog("ERROR", "backend_snapshot_read_failed", "读取宿主后端状态失败", { error });
  });
}

/** 返回当前后端实例快照，供 React runtime 外部 store 订阅使用。 */
export function getBackendRuntimeSnapshot(): BackendRuntimeSnapshot {
  return runtimeSnapshot;
}

/** 返回当前后端生命周期状态快照，供启动页和状态横幅订阅使用。 */
export function getBackendStatusSnapshot(): BackendStatusSnapshot {
  return statusSnapshot;
}

/** 订阅后端地址或 generation 的变化（切换进程、端口或重启）。 */
export function subscribeBackendRuntime(listener: () => void): () => void {
  runtimeListeners.add(listener);
  return () => runtimeListeners.delete(listener);
}

/** 订阅后端生命周期状态的变化，不会触发 Assistant runtime listener。 */
export function subscribeBackendStatus(listener: () => void): () => void {
  statusListeners.add(listener);
  return () => statusListeners.delete(listener);
}

/**
 * 将宿主 supervisor 的终态转换为启动错误。
 *
 * supervisor 返回的 message 是后端启动事实的安全摘要；前端只在状态明确为 failed / stopped 时展示它，
 * 不根据网络超时或其它本地猜测伪造后端错误。
 */
export function getBackendStatusError(status: BackendStatus): Error | null {
  if (status.state === "failed") {
    const message = status.message.trim() || "本地 Agent 后端启动失败，请点击重试";
    return new UserFacingError(message);
  }
  if (status.state === "stopped") {
    return new UserFacingError("本地 Agent 后端未运行，请点击重试");
  }
  return null;
}

/** 仅允许开发期的桌面宿主展示后端重启入口。 */
export function canRestartBackendRuntime(): boolean {
  return import.meta.env.DEV && desktopHost !== null;
}

/**
 * 启动门禁：等到后端就绪，失败时抛出可展示的错误；地址经 `getBackendRuntimeSnapshot` 读取。
 *
 * 先按已接受的最新快照判定一次（订阅早已存在时，例如开发期热更新重跑门禁），再等推送：快照进入 ready
 * 即成功，进入 failed / stopped 即失败，其间的 starting（含崩溃恢复）继续等。这里不另设超时：宿主
 * supervisor 的每次启动尝试都有时限，用尽后必定推送 failed；前端再计时只会在宿主仍在重试时误报失败。
 */
export function initializeBackendRuntime(): Promise<void> {
  const host = desktopHost;
  if (!host) {
    applyDevelopmentReady();
    return Promise.resolve();
  }
  return new Promise<void>((resolve, reject) => {
    const settle = (snapshot: BackendSnapshot) => {
      if (snapshot.status.state === "ready" && snapshot.backendBaseUrl) {
        finish();
        resolve();
        return;
      }
      const error = getBackendStatusError(snapshot.status);
      if (error) {
        finish();
        reject(error);
      }
    };
    const finish = () => {
      hostSnapshotListeners.delete(settle);
    };
    hostSnapshotListeners.add(settle);
    if (acceptedSnapshot) settle(acceptedSnapshot);
    ensureHostSubscription(host);
  });
}

function applyDevelopmentReady(): void {
  if (statusSnapshot?.state === "ready") return;
  statusSnapshot = { state: "ready" };
  for (const listener of statusListeners) listener();
}

/**
 * 运行期唯一的前端重启入口；并发调用复用同一次宿主重启。宿主的 restart 只在就绪时返回（否则抛出），
 * 成功即已应用新快照；失败时宿主已把 failed 快照推给横幅，这里把同一原因作为错误抛出。
 */
export function restartBackendRuntime(): Promise<void> {
  if (restartInFlight) return restartInFlight;
  // 只有桌面宿主里才有重启入口（启动门禁的重试与开发期重启按钮都依赖宿主）。
  const host = desktopHost!;
  ensureHostSubscription(host);
  const promise = host.backend.restart()
    .then(applyHostSnapshot, (error: unknown) => {
      // 宿主应答里的失败消息是 supervisor 给出的中文安全摘要，按用户可见错误抛出。
      throw error instanceof Error && error.message ? new UserFacingError(error.message) : error;
    })
    .finally(() => {
      if (restartInFlight === promise) restartInFlight = null;
    });
  restartInFlight = promise;
  return promise;
}
