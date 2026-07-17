// @vitest-environment happy-dom

/**
 * Sidebar 删除工作区确认弹窗组件测试。
 *
 * 验证本次交付的删除交互状态流转：
 * - 打开确认弹窗（点击删除按钮）
 * - 取消 / 点击遮罩关闭弹窗
 * - 确认后调用 api.deleteWorkspace 并同步 store、关闭弹窗
 * - 删除失败：保留弹窗、回显错误、logError 被调用
 * - 删除进行中禁用按钮
 *
 * 隔离外部依赖：mock @/services/api、@/lib/logger、@/hooks/useTask。
 * 使用真实 useWorkspaceStore / useTaskStore 驱动状态（仅依赖类型导入，运行时安全）。
 *
 * @module tests/sidebarDeleteConfirm
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceRecord } from "@shared/workspace";

// ---- mock 外部依赖（避免加载真实 api.ts 触发 @shared/api 值导入解析失败）----
const deleteWorkspaceMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@/services/api", () => ({
  deleteWorkspace: (...args: unknown[]) => deleteWorkspaceMock(...args),
}));

const logErrorMock = vi.fn();
vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => logErrorMock(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

// useTask 仅用于 openTask，本测试不触发，mock 掉避免其依赖链加载真实 api
vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({ openTask: vi.fn().mockResolvedValue(undefined) }),
}));

import { Sidebar } from "@/components/layout/Sidebar";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";

function makeWorkspace(id: string, name = `ws-${id}`): WorkspaceRecord {
  const now = new Date().toISOString();
  return { workspace_id: id, name, root_path: `/p/${id}`, created_at: now, updated_at: now };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  useWorkspaceStore.setState({
    workspaces: [makeWorkspace("a", "Alpha"), makeWorkspace("b", "Beta")],
    activeWorkspaceId: "a",
    collapsedWorkspaceIds: new Set<string>(),
  });
  useTaskStore.setState({ tasks: [], activeTaskId: null, activeTurnId: null });

  deleteWorkspaceMock.mockReset().mockResolvedValue(undefined);
  logErrorMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

function render(): void {
  act(() => {
    root.render(
      <Sidebar activeView="chat" onOpenLogs={vi.fn()} onOpenChat={vi.fn()} onNewTask={vi.fn()} />,
    );
  });
}

function clickDeleteButton(name: string): void {
  const buttons = Array.from(container.querySelectorAll("button[title='删除工作区']")) as HTMLButtonElement[];
  const target = buttons.find((b) => {
    // 找到同工作区行内的删除按钮：通过最近的父 div 文本匹配
    let el: HTMLElement | null = b.parentElement;
    while (el && el !== container) {
      if (el.textContent?.includes(name)) return true;
      el = el.parentElement;
    }
    return false;
  });
  expect(target, `未找到工作区 ${name} 的删除按钮`).toBeTruthy();
  act(() => {
    target!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

function getDialog(): HTMLElement | null {
  // 弹窗是 fixed inset-0 的遮罩
  return container.querySelector(".fixed.inset-0") as HTMLElement | null;
}

describe("Sidebar — 删除确认弹窗状态流转", () => {
  it("点击删除按钮打开确认弹窗并显示工作区名称", () => {
    render();
    expect(getDialog()).toBeNull();
    clickDeleteButton("Alpha");
    const dialog = getDialog();
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("Alpha");
    expect(dialog!.textContent).toContain("确定删除工作区");
  });

  it("点击取消关闭弹窗", () => {
    render();
    clickDeleteButton("Alpha");
    expect(getDialog()).not.toBeNull();
    const cancelBtn = Array.from(getDialog()!.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "取消",
    ) as HTMLButtonElement;
    act(() => {
      cancelBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(getDialog()).toBeNull();
  });

  it("点击遮罩（非删除中）关闭弹窗", () => {
    render();
    clickDeleteButton("Beta");
    const dialog = getDialog();
    expect(dialog).not.toBeNull();
    act(() => {
      dialog!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(getDialog()).toBeNull();
  });

  it("确认删除成功：调用 api.deleteWorkspace 并移除本地工作区、关闭弹窗", async () => {
    render();
    clickDeleteButton("Alpha");
    const confirmBtn = Array.from(getDialog()!.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "删除",
    ) as HTMLButtonElement;
    await act(async () => {
      confirmBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(deleteWorkspaceMock).toHaveBeenCalledWith("a");
    expect(useWorkspaceStore.getState().workspaces.map((w) => w.workspace_id)).toEqual(["b"]);
    expect(getDialog()).toBeNull();
  });

  it("确认删除失败：保留弹窗、回显错误、logError 被调用", async () => {
    deleteWorkspaceMock.mockRejectedValueOnce(new Error("backend down"));
    render();
    clickDeleteButton("Alpha");
    const confirmBtn = Array.from(getDialog()!.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "删除",
    ) as HTMLButtonElement;
    await act(async () => {
      confirmBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    // 弹窗保留
    const dialog = getDialog();
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("backend down");
    // 工作区未被移除
    expect(useWorkspaceStore.getState().workspaces).toHaveLength(2);
    // 错误经统一日志出口
    expect(logErrorMock).toHaveBeenCalledWith(
      "删除工作区失败",
      expect.any(Error),
      expect.objectContaining({ module: "Sidebar", workspace_id: "a" }),
    );
  });

  it("删除进行中确认按钮与取消按钮均禁用", async () => {
    // 让 deleteWorkspace 永不 resolve，保持 deleting 状态
    deleteWorkspaceMock.mockReturnValue(new Promise<void>(() => {}));
    render();
    clickDeleteButton("Alpha");
    const dialog = getDialog()!;
    const confirmBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("删除"),
    ) as HTMLButtonElement;
    // 点击确认，触发 handleConfirmDelete：同步 setDeleting(true) 后 await 挂起
    await act(async () => {
      confirmBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      // 让微任务队列推进，使 setDeleting(true) 的同步 setState 完成重渲染
      await Promise.resolve();
      await Promise.resolve();
    });
    // 重渲染后重新查询按钮节点（旧引用可能已被 React 替换）
    const dialogAfter = getDialog()!;
    const confirmBtnAfter = Array.from(dialogAfter.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("删除"),
    ) as HTMLButtonElement;
    const cancelBtnAfter = Array.from(dialogAfter.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "取消",
    ) as HTMLButtonElement;
    expect(confirmBtnAfter.disabled).toBe(true);
    expect(cancelBtnAfter.disabled).toBe(true);
    expect(confirmBtnAfter.textContent).toContain("删除中");
  });
});
