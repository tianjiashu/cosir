import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import {
  getBackendStatusError,
  getBackendRuntimeSnapshot,
  getBackendStatusSnapshot,
  initializeBackendRuntime,
  refreshBackendRuntime,
  restartBackendRuntime,
  startBackendRuntimeMonitor,
  subscribeBackendRuntime,
  subscribeBackendStatus,
  type BackendStatus,
} from "@/src/runtime-config";

const invokeMock = vi.mocked(invoke);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function enableTauriRuntime() {
  vi.stubGlobal("window", {
    __TAURI_INTERNALS__: {},
    __COSIR_RUNTIME_CONFIG__: undefined,
    setTimeout: globalThis.setTimeout,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
  });
}

describe("backend runtime startup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    enableTauriRuntime();
  });

  it("fails immediately when the supervisor reports a safe failure", async () => {
    invokeMock.mockResolvedValueOnce({
      state: "failed",
      message: "本地 Agent 后端启动失败，请点击重试",
    });

    await expect(initializeBackendRuntime()).rejects.toThrow("请点击重试");
    expect(invokeMock).toHaveBeenCalledWith("backend_status");
    expect(invokeMock).not.toHaveBeenCalledWith("backend_runtime_config");
  });

  it("uses the runtime config only after backend_status is ready", async () => {
    invokeMock
      .mockResolvedValueOnce({ state: "starting" })
      .mockResolvedValueOnce({ state: "ready" })
      .mockResolvedValueOnce({
        backendBaseUrl: "http://127.0.0.1:49152/",
        generation: 1,
        status: { state: "ready" },
      });

    await expect(initializeBackendRuntime()).resolves.toBe("http://127.0.0.1:49152");
    expect(invokeMock.mock.calls.map(([command]) => command)).toEqual([
      "backend_status",
      "backend_status",
      "backend_runtime_config",
    ]);
    expect(getBackendStatusError({ state: "ready" })).toBeNull();
  });

  it("keeps supervisor messages and does not invent failure details", () => {
    const failed: BackendStatus = { state: "failed", message: "启动失败：配置无效" };
    expect(getBackendStatusError(failed)?.message).toBe("启动失败：配置无效");
    const emptyMessageError = getBackendStatusError({ state: "failed", message: "" });
    expect(emptyMessageError?.message).toContain("请点击重试");
    expect(getBackendStatusError({ state: "stopped" })?.message).toContain("未运行");
  });

  it("publishes a new generation when a restarted backend reuses its URL", () => {
    invokeMock.mockResolvedValueOnce({
      backendBaseUrl: "http://127.0.0.1:49152",
      generation: 9,
      status: { state: "ready" },
    });

    return restartBackendRuntime().then(() => {
      const before = getBackendRuntimeSnapshot();
      invokeMock.mockResolvedValueOnce({
        backendBaseUrl: "http://127.0.0.1:49152",
        generation: before.generation + 1,
        status: { state: "ready" },
      });

      return restartBackendRuntime().then(() => {
        const after = getBackendRuntimeSnapshot();
        expect(after.backendBaseUrl).toBe(before.backendBaseUrl);
        expect(after.generation).toBe(before.generation + 1);
      });
    });
  });

  it("ignores a stale runtime config response", async () => {
    const previous = getBackendRuntimeSnapshot();
    const acceptedGeneration = previous.generation + 2;
    invokeMock.mockResolvedValueOnce({
      backendBaseUrl: "http://127.0.0.1:49152",
      generation: acceptedGeneration,
      status: { state: "ready" },
    });
    await restartBackendRuntime();

    invokeMock.mockResolvedValueOnce({
      backendBaseUrl: "http://127.0.0.1:49153",
      generation: acceptedGeneration - 1,
      status: { state: "ready" },
    });
    await expect(restartBackendRuntime()).rejects.toThrow("无法应用");

    const current = getBackendRuntimeSnapshot();
    expect(current.backendBaseUrl).toBe("http://127.0.0.1:0");
    expect(current.generation).toBe(acceptedGeneration);
  });

  it("does not publish a ready status for a same-generation URL conflict", async () => {
    const current = getBackendRuntimeSnapshot();
    const generation = current.generation + 1;
    invokeMock.mockResolvedValueOnce({
      backendBaseUrl: "http://127.0.0.1:49155",
      generation,
      status: { state: "ready" },
    });
    await restartBackendRuntime();

    const statusBefore = getBackendStatusSnapshot();
    invokeMock
      .mockResolvedValueOnce({ state: "ready" })
      .mockResolvedValueOnce({
        backendBaseUrl: "http://127.0.0.1:49156",
        generation,
        status: { state: "ready" },
      });
    await refreshBackendRuntime();

    expect(getBackendRuntimeSnapshot()).toEqual({
      backendBaseUrl: "http://127.0.0.1:49155",
      generation,
      available: true,
    });
    expect(getBackendStatusSnapshot()).toEqual(statusBefore);
  });

  it("keeps status and runtime notifications independent", async () => {
    const runtimeListener = vi.fn();
    const statusListener = vi.fn();
    const unsubscribeRuntime = subscribeBackendRuntime(runtimeListener);
    const unsubscribeStatus = subscribeBackendStatus(statusListener);

    invokeMock.mockResolvedValueOnce({ state: "starting" });
    await refreshBackendRuntime();
    expect(statusListener).toHaveBeenCalledTimes(1);
    expect(runtimeListener).toHaveBeenCalledTimes(1);

    runtimeListener.mockClear();
    statusListener.mockClear();
    invokeMock.mockResolvedValueOnce({ state: "failed", message: "后端暂不可用" });
    await refreshBackendRuntime();
    expect(statusListener).toHaveBeenCalledTimes(1);
    expect(runtimeListener).not.toHaveBeenCalled();

    invokeMock
      .mockResolvedValueOnce({ state: "ready" })
      .mockResolvedValueOnce({
        backendBaseUrl: "http://127.0.0.1:49152",
        generation: getBackendRuntimeSnapshot().generation + 1,
        status: { state: "ready" },
      });
    await refreshBackendRuntime();
    expect(runtimeListener).toHaveBeenCalledTimes(1);
    expect(statusListener).toHaveBeenCalledTimes(2);
    expect(getBackendStatusSnapshot()?.state).toBe("ready");

    unsubscribeRuntime();
    unsubscribeStatus();
  });

  it("does not let an old initialize response overwrite a newer restart", async () => {
    const pendingStatus = deferred<BackendStatus>();
    invokeMock.mockReturnValueOnce(pendingStatus.promise);
    const oldInitialize = initializeBackendRuntime();
    await Promise.resolve();

    const nextGeneration = getBackendRuntimeSnapshot().generation + 1;
    const callsBeforeRestart = invokeMock.mock.calls.length;
    invokeMock.mockResolvedValueOnce({
      backendBaseUrl: "http://127.0.0.1:49154",
      generation: nextGeneration,
      status: { state: "ready" },
    });
    await restartBackendRuntime();

    pendingStatus.resolve({ state: "ready" });
    await expect(oldInitialize).rejects.toThrow("生命周期操作已被更新");
    expect(getBackendRuntimeSnapshot().backendBaseUrl).toBe("http://127.0.0.1:49154");
    expect(getBackendRuntimeSnapshot().generation).toBe(nextGeneration);
    expect(invokeMock.mock.calls.slice(callsBeforeRestart).map(([command]) => command)).toEqual([
      "restart_backend",
    ]);
  });

  it("publishes a failed status when restart cannot be completed", async () => {
    invokeMock.mockRejectedValueOnce(new Error("restart command failed"));

    await expect(restartBackendRuntime()).rejects.toThrow("restart command failed");
    expect(getBackendStatusSnapshot()).toEqual({
      state: "failed",
      message: "restart command failed",
    });
    expect(getBackendRuntimeSnapshot().backendBaseUrl).toBe("http://127.0.0.1:0");
  });

  it("starts only one monitor and cleanup invalidates its refresh", () => {
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");

    const dispose = startBackendRuntimeMonitor();
    expect(startBackendRuntimeMonitor()).toBe(dispose);
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);

    dispose();
    dispose();
    expect(clearIntervalSpy).toHaveBeenCalledTimes(1);

    setIntervalSpy.mockRestore();
    clearIntervalSpy.mockRestore();
  });
});
