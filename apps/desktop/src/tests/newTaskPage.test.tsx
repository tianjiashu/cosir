// @vitest-environment happy-dom

/**
 * NewTaskPage 组件测试（Codex 风格重写）。
 *
 * 验证本次交付的交互逻辑：
 * - 未选工作区时快捷卡片 / 发送按钮禁用
 * - 选择工作区后快捷卡片点击、底部输入发送均触发 createTask
 * - 工作区选择器：打开菜单、选择已有、新建空白项目 / 使用现有文件夹直接弹系统目录选择器
 * - 目录选择器：选择目录 -> createWorkspace({name: 目录名, root_path}) + 选中 + 关闭菜单
 * - 目录选择器：取消选择不创建、选择失败回显错误并 logError
 * - 边界：operation.loading 时快捷卡片 / 发送禁用
 *
 * 隔离外部依赖：mock @/hooks/useTask、@/services/api、@/lib/logger、@tauri-apps/plugin-dialog。
 * 使用真实 useWorkspaceStore 驱动状态（仅依赖类型导入，运行时安全）。
 *
 * @module tests/newTaskPage
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceRecord } from "@shared/workspace";

const createTaskMock = vi.fn().mockResolvedValue(undefined);
const createWorkspaceMock = vi.fn().mockResolvedValue(undefined);
const logErrorMock = vi.fn();
const openMock = vi.fn();

// 可动态切换的 operation 状态
let operationState = { loading: false, error: null as string | null };

vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({ createTask: (...args: unknown[]) => createTaskMock(...args), operation: operationState }),
}));

vi.mock("@/services/api", () => ({
  createWorkspace: (...args: unknown[]) => createWorkspaceMock(...args),
}));

vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => logErrorMock(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (...args: unknown[]) => openMock(...args),
}));

import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import { useWorkspaceStore } from "@/stores/workspaceStore";

function makeWorkspace(id: string, name = `ws-${id}`): WorkspaceRecord {
  const now = new Date().toISOString();
  return { workspace_id: id, name, root_path: `/p/${id}`, created_at: now, updated_at: now };
}

let container: HTMLDivElement;
let root: Root;
let onCreated: ReturnType<typeof vi.fn>;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  onCreated = vi.fn();

  useWorkspaceStore.setState({
    workspaces: [makeWorkspace("a", "Alpha"), makeWorkspace("b", "Beta")],
    activeWorkspaceId: null,
    collapsedWorkspaceIds: new Set<string>(),
  });

  createTaskMock.mockReset().mockResolvedValue(undefined);
  createWorkspaceMock.mockReset().mockResolvedValue(makeWorkspace("new", "NewProj"));
  logErrorMock.mockReset();
  openMock.mockReset();
  operationState = { loading: false, error: null };
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

function render(): void {
  act(() => {
    root.render(<NewTaskPage onCreated={onCreated} />);
  });
}

function getButtonIncludes(text: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(text),
  ) as HTMLButtonElement | undefined;
}

function queryInput(placeholder: string): HTMLInputElement | undefined {
  return Array.from(container.querySelectorAll("input")).find(
    (i) => i.getAttribute("placeholder") === placeholder,
  ) as HTMLInputElement | undefined;
}

/** 底部输入区右侧的发送按钮（Input 的父容器内的 button）。 */
function getSendButton(): HTMLButtonElement {
  const input = queryInput("描述这次任务...")!;
  return input.parentElement!.querySelector("button") as HTMLButtonElement;
}

/**
 * 设置受控 input 的值并触发 React onChange。
 * 必须经由原生 value setter，否则 React 的 value tracker 不会感知变化。
 */
function setInputValue(input: HTMLInputElement, value: string): void {
  const proto = window.HTMLInputElement.prototype as unknown as {
    value: string;
  };
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) {
    setter.call(input, value);
  } else {
    input.value = value;
  }
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

/** 打开工作区菜单。 */
function openMenu(): void {
  act(() => {
    (getButtonIncludes("选择工作区") ?? getButtonIncludes("Alpha")!).dispatchEvent(
      new MouseEvent("click", { bubbles: true }),
    );
  });
}

describe("NewTaskPage — 工作区未选中时的边界", () => {
  it("未选工作区时四象限快捷卡片全部禁用", () => {
    render();
    const suggestions = ["探索并理解代码", "构建新功能、应用或工具", "审查代码并提出修改建议", "修复问题和失败"];
    for (const label of suggestions) {
      const btn = getButtonIncludes(label);
      expect(btn, `未找到卡片: ${label}`).toBeTruthy();
      expect(btn!.disabled).toBe(true);
    }
  });

  it("未选工作区时底部发送按钮禁用", () => {
    render();
    const input = queryInput("描述这次任务...");
    act(() => {
      setInputValue(input!, "hello");
    });
    const sendBtn = getSendButton();
    expect(sendBtn.disabled).toBe(true);
  });

  it("未选工作区时点击快捷卡片不触发 createTask", () => {
    render();
    const btn = getButtonIncludes("探索并理解代码")!;
    act(() => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(createTaskMock).not.toHaveBeenCalled();
  });
});

describe("NewTaskPage — 选择工作区后创建任务", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({ activeWorkspaceId: "a" });
  });

  it("工作区选中后快捷卡片可点击并触发 createTask", async () => {
    render();
    const btn = getButtonIncludes("探索并理解代码")!;
    expect(btn.disabled).toBe(false);
    await act(async () => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(createTaskMock).toHaveBeenCalledWith("探索并理解代码", "a");
    expect(onCreated).toHaveBeenCalled();
  });

  it("底部输入发送触发 createTask 并清空输入", async () => {
    render();
    const input = queryInput("描述这次任务...")!;
    act(() => {
      setInputValue(input, "写一个登录页");
    });
    const sendBtn = getSendButton();
    expect(sendBtn.disabled).toBe(false);
    await act(async () => {
      sendBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(createTaskMock).toHaveBeenCalledWith("写一个登录页", "a");
    expect(input.value).toBe("");
    expect(onCreated).toHaveBeenCalled();
  });

  it("operation.loading 时快捷卡片与发送按钮禁用", () => {
    operationState = { loading: true, error: null };
    render();
    const btn = getButtonIncludes("探索并理解代码")!;
    expect(btn.disabled).toBe(true);
    const sendBtn = getSendButton();
    expect(sendBtn.disabled).toBe(true);
  });
});

describe("NewTaskPage — 工作区选择器与目录选择", () => {
  it("点击工作区选择器打开菜单并列出已有工作区", () => {
    render();
    openMenu();
    expect(container.textContent).toContain("已有工作区");
    expect(container.textContent).toContain("Alpha");
    expect(container.textContent).toContain("Beta");
    expect(container.textContent).toContain("新建空白项目");
    expect(container.textContent).toContain("使用现有文件夹");
  });

  it("选择已有工作区后 setActiveWorkspace 并更新标题", () => {
    render();
    openMenu();
    const betaBtn = getButtonIncludes("Beta")!;
    act(() => {
      betaBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("b");
    expect(container.textContent).toContain("Beta");
  });

  it("新建空白项目：选择目录后 createWorkspace + 选中 + 关闭菜单", async () => {
    openMock.mockResolvedValue("/Users/me/MyProj");
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("新建空白项目")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(openMock).toHaveBeenCalledWith({ directory: true, multiple: false });
    expect(createWorkspaceMock).toHaveBeenCalledWith({ name: "MyProj", root_path: "/Users/me/MyProj" });
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("new");
    // 菜单已关闭，目录选择项不再可见
    expect(getButtonIncludes("新建空白项目")).toBeUndefined();
  });

  it("使用现有文件夹：选择目录后 createWorkspace（name 取目录名）", async () => {
    openMock.mockResolvedValue("/existing/folder");
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("使用现有文件夹")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(createWorkspaceMock).toHaveBeenCalledWith({ name: "folder", root_path: "/existing/folder" });
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("new");
  });

  it("目录选择取消：不创建工作区、菜单保持打开", async () => {
    openMock.mockResolvedValue(null);
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("新建空白项目")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(createWorkspaceMock).not.toHaveBeenCalled();
    expect(getButtonIncludes("新建空白项目")).toBeTruthy();
  });

  it("使用现有文件夹：同样调用 open({ directory: true, multiple: false })", async () => {
    openMock.mockResolvedValue("/existing/folder");
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("使用现有文件夹")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(openMock).toHaveBeenCalledWith({ directory: true, multiple: false });
  });

  it("目录名为 Windows 风格路径时 name 取末级目录名", async () => {
    openMock.mockResolvedValue("C:\\Users\\me\\WinProj");
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("新建空白项目")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(createWorkspaceMock).toHaveBeenCalledWith({ name: "WinProj", root_path: "C:\\Users\\me\\WinProj" });
  });

  it("目录选择失败：回显错误、logError 被调用、菜单保持打开", async () => {
    openMock.mockRejectedValue(new Error("no dialog"));
    render();
    openMenu();
    await act(async () => {
      getButtonIncludes("新建空白项目")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(container.textContent).toContain("no dialog");
    expect(logErrorMock).toHaveBeenCalledWith(
      "选择目录作为工作区失败",
      expect.any(Error),
      expect.objectContaining({ module: "NewTaskPage" }),
    );
    expect(getButtonIncludes("新建空白项目")).toBeTruthy();
  });
});
