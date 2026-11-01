import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-supervisor-"));

vi.mock("@/electron/main/paths", () => ({
  appLogDir: () => path.join(tempRoot, "logs"),
  runtimeDir: () => path.join(tempRoot, "runtime"),
  systemDataRoot: () => tempRoot,
}));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/backend/runtime", () => ({
  resolveBackendRuntime: vi.fn(async () => ({ mode: "python-module", launcher: "python", backendDir: tempRoot, terminalWorker: "worker" })),
}));
vi.mock("@/electron/main/backend/process", () => ({ spawnBackend: vi.fn(), stopProcessTree: vi.fn(async () => undefined) }));
vi.mock("@/electron/main/backend/readiness", () => ({ waitForBackend: vi.fn() }));

import { spawnBackend, stopProcessTree } from "@/electron/main/backend/process";
import { waitForBackend } from "@/electron/main/backend/readiness";
import { BackendSupervisor } from "@/electron/main/backend/supervisor";

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  pid = 4242;
  crash() {
    this.exitCode = 1;
    this.emit("exit", 1, null);
  }
}

const spawnMock = vi.mocked(spawnBackend);
const waitMock = vi.mocked(waitForBackend);
let children: FakeChild[] = [];

beforeEach(() => {
  vi.clearAllMocks();
  children = [];
  spawnMock.mockImplementation(() => {
    const child = new FakeChild();
    children.push(child);
    return child as never;
  });
  waitMock.mockResolvedValue({ ok: true });
});

/** 等待由事件触发的异步恢复（再次启动）走完。 */
async function settle() {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe("BackendSupervisor", () => {
  it("publishes ready with the backend address after a successful start", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    const snapshots: unknown[] = [];
    supervisor.onChange((snapshot) => snapshots.push(snapshot.status.state));

    await supervisor.start();

    const snapshot = supervisor.snapshot();
    expect(snapshot.status).toEqual({ state: "ready" });
    expect(snapshot.backendBaseUrl).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(snapshot.generation).toBe(1);
    expect(snapshots).toEqual(["starting", "ready"]);
    // 宿主工具链路的 token 随每次拉起注入后端；不注入时后端拒绝宿主连接。
    expect(spawnMock).toHaveBeenCalledWith(expect.objectContaining({ hostToken: "test-host-token" }));
  });

  // 目的：自动恢复只有一次；潜在缺陷：反复崩溃被无限重启掩盖，用户永远看不到失败。
  it("recovers from a crash once, then reports failure instead of restarting forever", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    await supervisor.start();

    children[0].crash();
    await settle();
    expect(supervisor.snapshot().status).toEqual({ state: "ready" });
    expect(supervisor.snapshot().generation).toBe(2);

    children[1].crash();
    await settle();
    expect(supervisor.snapshot().status).toEqual({ state: "failed", message: "本地 Agent 后端已退出，请点击重试" });
    expect(spawnMock).toHaveBeenCalledTimes(2);
  });

  it("restart resets the crash recovery budget", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    await supervisor.start();
    children[0].crash();
    await settle();
    children[1].crash();
    await settle();

    await expect(supervisor.restart()).resolves.toMatchObject({ status: { state: "ready" } });
    children[2].crash();
    await settle();

    expect(supervisor.snapshot().status).toEqual({ state: "ready" });
    expect(spawnMock).toHaveBeenCalledTimes(4);
  });

  it("retries a failed health check within one start and stops the failed process", async () => {
    waitMock
      .mockResolvedValueOnce({ ok: false, superseded: false, message: "后端在 90 秒内未通过健康检查" })
      .mockResolvedValueOnce({ ok: false, superseded: false, message: "后端在 90 秒内未通过健康检查" })
      .mockResolvedValueOnce({ ok: true });
    const supervisor = new BackendSupervisor("test-host-token");

    await supervisor.start();

    expect(supervisor.snapshot().status).toEqual({ state: "ready" });
    expect(spawnMock).toHaveBeenCalledTimes(3);
    expect(stopProcessTree).toHaveBeenCalledTimes(2);
  });

  it("reports the last attempt's reason once every attempt fails", async () => {
    waitMock.mockResolvedValue({ ok: false, superseded: false, message: "本地 Agent 后端启动失败（ImportError）：缺少依赖" });
    const supervisor = new BackendSupervisor("test-host-token");

    await supervisor.start();

    expect(supervisor.snapshot().status).toEqual({ state: "failed", message: "本地 Agent 后端启动失败（ImportError）：缺少依赖" });
    expect(spawnMock).toHaveBeenCalledTimes(3);
  });

  // 目的：被取代的启动不再发布状态；潜在缺陷：退出过程中迟到的「就绪」把已停止的后端标回 ready。
  it("does not publish a superseded start after stop", async () => {
    let release!: () => void;
    waitMock.mockImplementationOnce(async ({ isCurrent }) => {
      await new Promise<void>((resolve) => (release = resolve));
      return isCurrent() ? { ok: true } : { ok: false, superseded: true };
    });
    const supervisor = new BackendSupervisor("test-host-token");
    const starting = supervisor.start();
    await settle();

    await supervisor.stop();
    release();
    await starting;

    expect(supervisor.snapshot().status).toEqual({ state: "stopped" });
    expect(stopProcessTree).toHaveBeenCalledWith(children[0]);
  });

  // 目的：宿主退出是终态。潜在缺陷：stop() 之后的重启把正在退出的宿主的后端重新拉起，留下孤儿进程。
  it("never launches the backend again after stop", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    await supervisor.start();

    await supervisor.stop();
    await expect(supervisor.restart()).rejects.toThrow();

    expect(spawnMock).toHaveBeenCalledTimes(1);
  });

  it("does not treat an intentional stop as a crash", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    await supervisor.start();

    await supervisor.stop();
    children[0].crash();
    await settle();

    expect(supervisor.snapshot().status).toEqual({ state: "stopped" });
    expect(spawnMock).toHaveBeenCalledTimes(1);
  });

  it("refuses to restart while a start is in flight", async () => {
    let release!: () => void;
    waitMock.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (release = resolve));
      return { ok: true };
    });
    const supervisor = new BackendSupervisor("test-host-token");
    const starting = supervisor.start();
    await settle();

    await expect(supervisor.restart()).rejects.toThrow("正在启动");
    release();
    await starting;
  });

  // 目的：停旧进程的宽限期也受启动守卫保护；潜在缺陷：连点重启时第二次趁旧进程还没退出插进来，
  // 第一次醒来覆盖并清掉守卫，留下无人看守的启动，还向用户报「重启失败」。
  it("refuses a second restart while the first is still stopping the old process", async () => {
    const supervisor = new BackendSupervisor("test-host-token");
    await supervisor.start();
    let releaseStop!: () => void;
    vi.mocked(stopProcessTree).mockImplementationOnce(() => new Promise<void>((resolve) => (releaseStop = resolve)));

    const first = supervisor.restart();
    await expect(supervisor.restart()).rejects.toThrow("正在启动");
    releaseStop();

    await expect(first).resolves.toMatchObject({ status: { state: "ready" } });
    expect(spawnMock).toHaveBeenCalledTimes(2);
  });
});
