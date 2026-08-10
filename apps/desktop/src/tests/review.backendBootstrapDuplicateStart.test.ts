// @vitest-environment happy-dom
/**
 * 缺陷验证 #6：useBackendBootstrap 级联重触发导致 backend_start 重复调用。
 *
 * 背景：bootstrap effect 依赖 ensureRunning；而 ensureRunning 经 refreshStatus
 * 依赖 store 的 status（useBackend.ts 112-114 行：refreshStatus 的 deps 含 status）。
 * ensureRunning 自身执行时会改写 status（stopped→starting→...→running），
 * 每次 status 变化 → ensureRunning 换新引用 → bootstrap effect 重触发 →
 * 新一轮 refreshStatus/start。当多个 ensureRunning 并发在途且均查询到
 * 「后端未运行」时，会重复发起 backend_start。
 *
 * 注意：本测试使用非 StrictMode 渲染，验证的是「状态变化引发级联重触发」
 * 这一机制本身，而非 StrictMode 的双调语义。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { BackendStatusResponse } from "@shared/backend";

const backendMocks = vi.hoisted(() => ({
  getBackendStatus: vi.fn(),
  startBackend: vi.fn(),
  stopBackend: vi.fn(),
  restartBackend: vi.fn(),
  tailBackendLogs: vi.fn(),
  openFileInEditor: vi.fn(),
}));

// mock 后端 IPC 封装层：status/start 均替换为手动可控的 deferred 桩。
vi.mock("@/services/backend", () => backendMocks);
vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

import { useBackendBootstrap } from "@/hooks/useBackendBootstrap";
import { useBackendStore } from "@/stores/backendStore";

function snapshot(status: string): BackendStatusResponse {
  return { status, managed: true, pid: 1234 } as unknown as BackendStatusResponse;
}

/** 把 promise 链冲刷若干拍，让已 resolve 的 continuation 全部跑完。 */
async function flushMicrotasks(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await Promise.resolve();
  }
}

describe("useBackendBootstrap 级联重触发", () => {
  let statusResolvers: Array<(v: BackendStatusResponse) => void>;
  let startResolvers: Array<(v: BackendStatusResponse) => void>;

  beforeEach(() => {
    vi.clearAllMocks();
    useBackendStore.getState().reset();
    statusResolvers = [];
    startResolvers = [];
    backendMocks.getBackendStatus.mockImplementation(
      () => new Promise<BackendStatusResponse>((resolve) => statusResolvers.push(resolve)),
    );
    backendMocks.startBackend.mockImplementation(
      () => new Promise<BackendStatusResponse>((resolve) => startResolvers.push(resolve)),
    );
  });

  // 测试目的：非 StrictMode 下，一次 bootstrap 生命周期内 backend_start 只应调用一次。
  // 可能发现的缺陷：status 变化 → ensureRunning 换引用 → effect 重触发 →
  //   并发 ensureRunning 均判定「未运行」→ 重复 backend_start。
  it("后端初始未运行时，bootstrap 只应触发一次 backend_start", async () => {
    renderHook(() => useBackendBootstrap());
    await act(async () => {
      await flushMicrotasks();
    });

    // 机制守卫（修复后语义反转）：refreshStatus 引用已稳定（不再依赖 status 闭包），
    // status stopped→starting 不得引发级联重触发，在途 status 查询应恰好 1 个。
    // 若级联回归（>1），说明 ensureRunning 引用再次随 status 变化。
    const pendingStatus = statusResolvers.splice(0);
    expect(pendingStatus.length).toBe(1);

    // 两个并发 ensureRunning 均查询到「后端未运行」（真实场景：start 尚未完成）。
    await act(async () => {
      for (const resolve of pendingStatus) {
        resolve(snapshot("stopped"));
      }
      await flushMicrotasks();
    });

    // 收尾：让所有在途 start 完成，避免悬挂 promise。
    await act(async () => {
      for (const resolve of startResolvers.splice(0)) {
        resolve(snapshot("running"));
      }
      await flushMicrotasks();
    });

    // 正确行为：整个 bootstrap 只启动一次后端。
    expect(backendMocks.startBackend).toHaveBeenCalledTimes(1);
  });

  // 测试目的：正向对照——后端已在运行时，bootstrap 不应发起任何 start。
  // 可能发现的缺陷：无（此用例应 PASS，证明 mock 链路与判定通路正确）。
  it("对照：后端已在运行时，bootstrap 不调用 backend_start", async () => {
    renderHook(() => useBackendBootstrap());
    await act(async () => {
      await flushMicrotasks();
    });
    const pendingStatus = statusResolvers.splice(0);
    expect(pendingStatus.length).toBeGreaterThanOrEqual(1);
    await act(async () => {
      for (const resolve of pendingStatus) {
        resolve(snapshot("running"));
      }
      await flushMicrotasks();
    });
    expect(backendMocks.startBackend).not.toHaveBeenCalled();
  });
});
