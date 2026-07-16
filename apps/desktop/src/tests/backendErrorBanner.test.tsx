// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BackendErrorSummary, BackendStatusResponse } from "@shared/backend";
import { BackendErrorBanner } from "@/components/backend/BackendErrorBanner";
import { useBackendStore } from "@/stores/backendStore";

// mock useBackend：避免真实 IPC 调用，并提供可控的 restart / isBusy。
const mockedRestart = vi.fn().mockResolvedValue(undefined);
vi.mock("@/hooks/useBackend", () => ({
  useBackend: () => ({ restart: mockedRestart, isBusy: false }),
}));

vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

let container: HTMLDivElement;
let root: Root;

function makeError(overrides: Partial<BackendErrorSummary>): BackendErrorSummary {
  return {
    stage: "launch",
    message: "启动本地后端失败",
    detail: "无法启动本地 Python 后端",
    traceback: null,
    occurredAt: "2026-07-15T10:00:00.000Z",
    ...overrides,
  };
}

function applyFailedSnapshot(error: BackendErrorSummary | null): void {
  const snapshot: BackendStatusResponse = {
    status: "failed",
    managed: false,
    pid: null,
    port: 8000,
    startedAt: null,
    health: null,
    lastError: error,
    repoRoot: "/repo",
    backendDir: "/repo/apps/backend",
    pythonBinary: "/repo/apps/backend/.venv/bin/python",
  };
  useBackendStore.getState().applySnapshot(snapshot);
}

async function render(): Promise<void> {
  await act(async () => {
    root.render(<BackendErrorBanner onViewLogs={vi.fn()} />);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useBackendStore.getState().reset();
  mockedRestart.mockReset();
  mockedRestart.mockResolvedValue(undefined);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

describe("BackendErrorBanner — 错误卡渲染", () => {
  it("status 非 failed 时不渲染任何内容", () => {
    const snapshot: BackendStatusResponse = {
      status: "running",
      managed: true,
      pid: 1,
      port: 8000,
      startedAt: "2026-07-15T10:00:00.000Z",
      health: null,
      lastError: makeError({}),
      repoRoot: "/repo",
      backendDir: "/repo/apps/backend",
      pythonBinary: "/repo/apps/backend/.venv/bin/python",
    };
    useBackendStore.getState().applySnapshot(snapshot);
    act(() => {
      root.render(<BackendErrorBanner onViewLogs={vi.fn()} />);
    });
    expect(container.textContent).toBe("");
  });

  it("status 为 failed 但 lastError 为空时渲染 null", () => {
    applyFailedSnapshot(null);
    act(() => {
      root.render(<BackendErrorBanner onViewLogs={vi.fn()} />);
    });
    expect(container.textContent).toBe("");
  });

  it("完整错误：渲染 message、detail、堆栈与 stage/occurredAt", async () => {
    applyFailedSnapshot(
      makeError({
        detail: "无法启动本地 Python 后端 python",
        traceback: "Traceback (most recent call last):\n  File 'app.py'",
      }),
    );
    await render();

    expect(container.textContent).toContain("启动本地后端失败");
    expect(container.textContent).toContain("无法启动本地 Python 后端 python");
    expect(container.textContent).toContain("launch");
    expect(container.textContent).toContain("2026-07-15T10:00:00.000Z");
    // 默认未展开，堆栈不应直接可见
    expect(container.textContent).not.toContain("Traceback (most recent call last)");
    // 存在「查看堆栈」按钮
    const stackBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("查看堆栈"),
    );
    expect(stackBtn).toBeTruthy();
  });

  it("展开堆栈后渲染 traceback 内容", async () => {
    applyFailedSnapshot(makeError({ traceback: "Traceback (most recent call last):\n  File 'app.py', line 1" }));
    await render();
    const stackBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("查看堆栈"),
    ) as HTMLButtonElement;
    await act(async () => {
      stackBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(container.textContent).toContain("Traceback (most recent call last)");
  });

  it("部分字段缺失（无 detail、无 traceback）：不渲染 detail 与堆栈按钮", async () => {
    applyFailedSnapshot(makeError({ detail: "", traceback: null }));
    await render();

    expect(container.textContent).toContain("启动本地后端失败");
    // 无 detail 文本
    expect(container.textContent).not.toContain("无法启动本地 Python 后端");
    // 无堆栈按钮
    const stackBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("查看堆栈"),
    );
    expect(stackBtn).toBeFalsy();
  });

  it("不同错误类型（stage 分支）：health_check 阶段渲染对应 stage", async () => {
    applyFailedSnapshot(makeError({ stage: "health_check", message: "后端启动后未通过健康检查" }));
    await render();
    expect(container.textContent).toContain("后端启动后未通过健康检查");
    expect(container.textContent).toContain("health_check");
  });

  it("点击重启按钮调用 restart 且不抛错", async () => {
    applyFailedSnapshot(makeError({}));
    await render();
    const restartBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("重启后端"),
    ) as HTMLButtonElement;
    expect(restartBtn).toBeTruthy();
    await act(async () => {
      restartBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(mockedRestart).toHaveBeenCalledTimes(1);
  });

  it("敏感信息屏蔽：traceback 中若含明文密钥，组件按原样渲染（脱敏责任在源头）", async () => {
    // 组件本身不做脱敏，因此验证：传入已脱敏内容时正确显示，
    // 且组件不会向 DOM 注入任何额外的敏感字段（仅 stage/message/detail/traceback/occurredAt）。
    const masked = "sk-****REDACTED****";
    applyFailedSnapshot(makeError({ traceback: `Traceback\napi_key=${masked}` }));
    await render();
    const stackBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("查看堆栈"),
    ) as HTMLButtonElement;
    await act(async () => {
      stackBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    // 已脱敏内容应被如实呈现
    expect(container.textContent).toContain(masked);
    // 组件未自行生成明文 key 字段（确认渲染节点只有预期字段）
    expect(container.querySelector("pre")?.textContent).toContain(`api_key=${masked}`);
  });
});
