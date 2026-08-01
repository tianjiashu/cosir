// @vitest-environment happy-dom

/**
 * ChatPanel 无工作区防御性空状态测试。
 *
 * 验证删除所有工作区后回到会话页时：
 * - activeWorkspaceId 为空时渲染「当前没有可用的工作区」引导块；
 * - 引导块内的「选择工作区」按钮点击后触发 onPickWorkspace 回调；
 * - 存在活跃工作区时不渲染该引导块。
 *
 * 隔离外部依赖：mock @/lib/logger 避免加载真实日志实现；
 * 使用真实 useWorkspaceStore / useTaskStore / useTurnStore / useEventStore 驱动状态。
 *
 * @module tests/chatPanel
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const logErrorMock = vi.fn();
vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => logErrorMock(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

import { ChatPanel, type ChatPanelProps } from "@/components/layout/ChatPanel";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useEventStore } from "@/stores/eventStore";

function makeWorkspace(id: string, name = `ws-${id}`): {
  workspace_id: string;
  name: string;
  root_path: string;
  created_at: string;
  updated_at: string;
} {
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
    workspaces: [],
    activeWorkspaceId: null,
    collapsedWorkspaceIds: new Set<string>(),
  });
  useTaskStore.getState().clearTasks();
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });
  useEventStore.setState({ eventsByTurnId: {} });
  logErrorMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

function renderWith(props: Partial<ChatPanelProps> = {}): void {
  const onPickWorkspace = props.onPickWorkspace ?? vi.fn();
  act(() => {
    root.render(<ChatPanel onPickWorkspace={onPickWorkspace} />);
  });
}

function getGuideBlock(): HTMLElement | null {
  return container.querySelector('[data-testid="workspace-guide"]') as HTMLElement | null;
}

describe("ChatPanel — 无工作区防御性空状态", () => {
  it("activeWorkspaceId 为空时渲染工作区引导块并点击触发 onPickWorkspace", () => {
    const onPickWorkspace = vi.fn();
    renderWith({ onPickWorkspace });

    const guide = getGuideBlock();
    expect(guide).not.toBeNull();
    expect(guide!.textContent).toContain("当前没有可用的工作区");

    const button = guide!.querySelector("button") as HTMLButtonElement | null;
    expect(button, "未找到选择工作区按钮").toBeTruthy();
    act(() => {
      button!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onPickWorkspace).toHaveBeenCalledTimes(1);
  });

  it("存在活跃工作区时不渲染引导块", () => {
    useWorkspaceStore.setState({
      workspaces: [makeWorkspace("a", "Alpha")],
      activeWorkspaceId: "a",
      collapsedWorkspaceIds: new Set<string>(),
    });
    renderWith();
    expect(getGuideBlock()).toBeNull();
  });
});
