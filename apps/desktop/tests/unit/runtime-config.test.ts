import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

import { invoke } from "@tauri-apps/api/core";
import {
  getBackendStatusError,
  initializeBackendRuntime,
  type BackendStatus,
} from "@/src/runtime-config";

const invokeMock = vi.mocked(invoke);

function enableTauriRuntime() {
  vi.stubGlobal("window", {
    __TAURI_INTERNALS__: {},
    __COSIR_RUNTIME_CONFIG__: undefined,
    setTimeout: globalThis.setTimeout,
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
});
