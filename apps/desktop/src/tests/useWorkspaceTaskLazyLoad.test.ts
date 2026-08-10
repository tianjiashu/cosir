// @vitest-environment happy-dom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// 屏蔽 logger 噪声，保留其余导出。
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

// 可变的 listWorkspaceTasks 实现：默认成功返回空；失败用例临时改写抛错。
const listWorkspaceTasks = vi.fn();
vi.mock("@/services/api", () => ({
  listWorkspaceTasks: (...args: unknown[]) => listWorkspaceTasks(...args),
}));

import { useTaskStore } from "@/stores/taskStore";
import { loadWorkspaceTasks, getFailedWorkspaceIds, useWorkspaceTaskLazyLoad } from "@/hooks/useWorkspaceTaskLazyLoad";

describe("loadWorkspaceTasks 加载失败态", () => {
  beforeEach(async () => {
    localStorage.clear();
    useTaskStore.getState().clearTasks();
    listWorkspaceTasks.mockReset();
    // 每个用例前清空模块级失败集合（clearTasks 不触碰它，故对残留失败项做一次成功加载）。
    const pending = [...getFailedWorkspaceIds()];
    for (const id of pending) {
      listWorkspaceTasks.mockResolvedValueOnce([]);
      await loadWorkspaceTasks(id);
    }
  });

  it("加载失败：返回 false 且记入 failedWorkspaceIds", async () => {
    listWorkspaceTasks.mockRejectedValueOnce(new Error("network"));
    const ok = await loadWorkspaceTasks("ws-fail");
    expect(ok).toBe(false);
    expect(getFailedWorkspaceIds().has("ws-fail")).toBe(true);
    // 失败不应污染分组缓存（保持未加载 undefined）。
    expect(useTaskStore.getState().tasksByWorkspaceId["ws-fail"]).toBeUndefined();
    expect(useTaskStore.getState().isWorkspaceLoaded("ws-fail")).toBe(false);
  });

  it("失败后重试成功：清空 failedWorkspaceIds 并完成加载", async () => {
    listWorkspaceTasks.mockRejectedValueOnce(new Error("network"));
    await loadWorkspaceTasks("ws-retry");
    expect(getFailedWorkspaceIds().has("ws-retry")).toBe(true);

    listWorkspaceTasks.mockResolvedValueOnce([{ task_id: "task-x", workspace_id: "ws-retry" } as never]);
    const ok = await loadWorkspaceTasks("ws-retry");
    expect(ok).toBe(true);
    expect(getFailedWorkspaceIds().has("ws-retry")).toBe(false);
    expect(useTaskStore.getState().isWorkspaceLoaded("ws-retry")).toBe(true);
  });

  it("渲染层：加载失败驱动 hook 返回的 failedWorkspaceIds 更新（不可变快照契约）", async () => {
    // 折叠态：effect 不会自动加载该 workspace，确保 act 内的 ensureLoaded 是唯一触发，
    // 能真实走失败分支（避免 effect 提前加载导致 skipped 跳过失败路径）。
    const workspaces = [{ workspace_id: "ws-render" }] as unknown as Parameters<typeof useWorkspaceTaskLazyLoad>[0];
    const collapsed = new Set<string>(["ws-render"]);
    const { result } = renderHook(() => useWorkspaceTaskLazyLoad(workspaces, collapsed));
    // 初次渲染：失败集合为空。
    expect(result.current.failedWorkspaceIds.has("ws-render")).toBe(false);

    listWorkspaceTasks.mockRejectedValueOnce(new Error("network"));
    await act(async () => {
      await result.current.ensureLoaded("ws-render");
    });

    // 失败态须经 useSyncExternalStore 不可变快照驱动重渲染，hook 返回值确实更新。
    await waitFor(() => {
      expect(result.current.failedWorkspaceIds.has("ws-render")).toBe(true);
    });
  });
});
