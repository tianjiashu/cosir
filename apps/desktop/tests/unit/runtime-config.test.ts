import { describe, expect, it, vi } from "vitest";

import type { BackendSnapshot } from "@/electron/shared/host-contract";

type Host = {
  backend: {
    snapshot: ReturnType<typeof vi.fn>;
    restart: ReturnType<typeof vi.fn>;
    onChange: (listener: (snapshot: BackendSnapshot) => void) => () => void;
  };
};

let push: (snapshot: BackendSnapshot) => void = () => undefined;
const host = vi.hoisted(() => ({ current: null as Host | null }));
vi.mock("@/lib/desktop-host", () => ({
  get desktopHost() {
    return host.current;
  },
}));

const ready = (generation: number, port = 49152): BackendSnapshot => ({
  generation,
  status: { state: "ready" },
  backendBaseUrl: `http://127.0.0.1:${port}`,
});

/** 每个用例重新加载模块：投影是模块级状态，用例之间不能互相污染。 */
async function loadWithHost(initial: BackendSnapshot) {
  host.current = {
    backend: {
      snapshot: vi.fn().mockResolvedValue(initial),
      restart: vi.fn(),
      onChange: (listener) => {
        push = listener;
        return () => undefined;
      },
    },
  };
  vi.resetModules();
  return { runtime: await import("@/lib/backend/runtime-config"), backend: host.current.backend };
}

describe("backend runtime projection", () => {
  it("resolves the boot gate once a pushed snapshot reports ready", async () => {
    const { runtime } = await loadWithHost({ generation: 1, status: { state: "starting" }, backendBaseUrl: null });
    const boot = runtime.initializeBackendRuntime();
    await Promise.resolve();
    push(ready(1));

    await expect(boot).resolves.toBeUndefined();
    expect(runtime.getBackendRuntimeSnapshot()).toEqual({ backendBaseUrl: "http://127.0.0.1:49152", generation: 1, available: true });
    expect(runtime.getBackendStatusSnapshot()).toEqual({ state: "ready" });
  });

  it("fails the boot gate with the supervisor's own message", async () => {
    const { runtime } = await loadWithHost({ generation: 1, status: { state: "failed", message: "启动失败：配置无效" }, backendBaseUrl: null });
    await expect(runtime.initializeBackendRuntime()).rejects.toThrow("启动失败：配置无效");
  });

  // 目的：订阅已存在时再次进入门禁（开发期热更新重跑）按已接受的快照立即判定；潜在缺陷：后端早已就绪，
  // 门禁却只等下一次推送，永远等不到。
  it("settles a repeated boot gate from the already accepted snapshot", async () => {
    const { runtime } = await loadWithHost(ready(1));
    await runtime.initializeBackendRuntime();

    await expect(runtime.initializeBackendRuntime()).resolves.toBeUndefined();
  });

  // 目的：旧实例的迟到快照不能覆盖新实例；潜在缺陷：重启后旧进程的 failed 推送把界面打回错误态。
  it("ignores a snapshot from an older generation", async () => {
    const { runtime } = await loadWithHost(ready(3));
    await runtime.initializeBackendRuntime();

    push({ generation: 2, status: { state: "failed", message: "旧实例退出" }, backendBaseUrl: null });

    expect(runtime.getBackendStatusSnapshot()).toEqual({ state: "ready" });
    expect(runtime.getBackendRuntimeSnapshot().generation).toBe(3);
  });

  // 目的：两份投影先一起更新再通知；潜在缺陷：订阅方在状态回调里读到旧地址或旧 generation。
  it("updates both projections before notifying, and only notifies what changed", async () => {
    const { runtime } = await loadWithHost(ready(1));
    await runtime.initializeBackendRuntime();
    const seen: string[] = [];
    runtime.subscribeBackendStatus(() => seen.push(`status:${runtime.getBackendRuntimeSnapshot().available}`));
    runtime.subscribeBackendRuntime(() => seen.push(`runtime:${runtime.getBackendStatusSnapshot()?.state}`));

    push({ generation: 2, status: { state: "starting" }, backendBaseUrl: null });
    push({ generation: 2, status: { state: "starting" }, backendBaseUrl: null });

    expect(seen).toEqual(["status:false", "runtime:starting"]);
    expect(runtime.getBackendRuntimeSnapshot().backendBaseUrl).toBe("http://127.0.0.1:0");
  });

  it("publishes a new generation when a restarted backend reuses its URL", async () => {
    const { runtime, backend } = await loadWithHost(ready(1));
    await runtime.initializeBackendRuntime();
    const changes = vi.fn();
    runtime.subscribeBackendRuntime(changes);
    backend.restart.mockResolvedValueOnce(ready(2));

    await runtime.restartBackendRuntime();

    expect(runtime.getBackendRuntimeSnapshot()).toMatchObject({ backendBaseUrl: "http://127.0.0.1:49152", generation: 2 });
    expect(changes).toHaveBeenCalledTimes(1);
  });

  it("shares one in-flight restart and surfaces the supervisor failure as a user-facing error", async () => {
    const { runtime, backend } = await loadWithHost(ready(1));
    backend.restart.mockRejectedValueOnce(new Error("本地 Agent 后端正在启动，请等待启动完成"));

    const first = runtime.restartBackendRuntime();
    const second = runtime.restartBackendRuntime();

    expect(second).toBe(first);
    await expect(first).rejects.toMatchObject({ name: "UserFacingError", message: "本地 Agent 后端正在启动，请等待启动完成" });
    expect(backend.restart).toHaveBeenCalledTimes(1);
  });

  it("keeps supervisor messages and does not invent failure details", async () => {
    const { runtime } = await loadWithHost(ready(1));
    expect(runtime.getBackendStatusError({ state: "ready" })).toBeNull();
    expect(runtime.getBackendStatusError({ state: "failed", message: "" })?.message).toContain("请点击重试");
    expect(runtime.getBackendStatusError({ state: "stopped" })?.message).toContain("未运行");
  });

  it("uses the development URL and a ready status outside the desktop host", async () => {
    host.current = null;
    vi.resetModules();
    const runtime = await import("@/lib/backend/runtime-config");

    await runtime.initializeBackendRuntime();
    expect(runtime.getBackendStatusSnapshot()).toEqual({ state: "ready" });
    expect(runtime.getBackendRuntimeSnapshot()).toMatchObject({ backendBaseUrl: "http://127.0.0.1:8000", available: true });
  });
});
