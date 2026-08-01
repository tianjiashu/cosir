// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InputBar } from "@/components/layout/InputBar";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { makeTask } from "@/tests/test-utils/factories";

const createTaskMock = vi.fn().mockResolvedValue(true);
const createTurnMock = vi.fn().mockResolvedValue(true);
const cancelTurnMock = vi.fn().mockResolvedValue(undefined);
const logErrorMock = vi.fn();

let operationState = { loading: false, error: null as string | null };

vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: (...args: unknown[]) => createTaskMock(...args),
    createTurn: (...args: unknown[]) => createTurnMock(...args),
    cancelTurn: (...args: unknown[]) => cancelTurnMock(...args),
    operation: operationState,
  }),
}));

vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => logErrorMock(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

vi.mock("@/services/api", () => ({
  listAgents: vi.fn(() => new Promise(() => undefined)),
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  useTaskStore.getState().clearTasks();
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });
  useWorkspaceStore.setState({
    workspaces: [],
    activeWorkspaceId: null,
    collapsedWorkspaceIds: new Set<string>(),
  });

  createTaskMock.mockReset().mockResolvedValue(true);
  createTurnMock.mockReset().mockResolvedValue(true);
  cancelTurnMock.mockReset().mockResolvedValue(undefined);
  logErrorMock.mockReset();
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
    root.render(<InputBar />);
  });
}

function getInput(): HTMLInputElement {
  const input = container.querySelector("input");
  if (!input) throw new Error("InputBar input not found");
  return input;
}

function getSendButton(): HTMLButtonElement {
  const button = container.querySelector('button[aria-label="发送"]');
  if (!button) throw new Error("InputBar send button not found");
  return button as HTMLButtonElement;
}

function getStopButton(): HTMLButtonElement {
  const button = container.querySelector('button[aria-label="停止当前轮次"]');
  if (!button) throw new Error("InputBar stop button not found");
  return button as HTMLButtonElement;
}

function setInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  if (setter) {
    setter.call(input, value);
  } else {
    input.value = value;
  }
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("InputBar — 发送入口", () => {
  it("未选任务但已选工作区时创建新任务", async () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    render();

    const input = getInput();
    act(() => {
      setInputValue(input, "你好");
    });

    await act(async () => {
      getSendButton().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(createTaskMock).toHaveBeenCalledWith("你好", "workspace-1");
    expect(createTurnMock).not.toHaveBeenCalled();
    expect(input.value).toBe("");
  });

  it("已有活跃任务时追加 turn", async () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    useTaskStore.getState().addTask(makeTask("task-1", { latest_turn_id: "turn-1" }));
    render();

    const input = getInput();
    act(() => {
      setInputValue(input, "继续");
    });

    await act(async () => {
      getSendButton().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(createTurnMock).toHaveBeenCalledWith("继续");
    expect(createTaskMock).not.toHaveBeenCalled();
    expect(input.value).toBe("");
  });

  it("没有任务且没有工作区时发送按钮禁用", () => {
    render();
    act(() => {
      setInputValue(getInput(), "你好");
    });
    expect(getSendButton().disabled).toBe(true);
  });

  it("无工作区时占位提示引导先选择工作区", () => {
    render();
    expect(getInput().getAttribute("placeholder")).toBe("请先选择工作区再开始对话...");
  });

  it("有工作区但无活跃任务时占位提示为开始对话", () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    render();
    expect(getInput().getAttribute("placeholder")).toBe("输入任务内容开始对话...");
  });

  it("操作加载中时发送按钮禁用", () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    operationState = { loading: true, error: null };
    render();
    act(() => {
      setInputValue(getInput(), "你好");
    });
    expect(getSendButton().disabled).toBe(true);
  });

  it("当前轮次还在接收事件流时显示停止按钮", async () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    useTurnStore.getState().setStreamingTurn("turn-1");
    render();
    act(() => {
      setInputValue(getInput(), "你好");
    });

    await act(async () => {
      getStopButton().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(cancelTurnMock).toHaveBeenCalledTimes(1);
    expect(createTaskMock).not.toHaveBeenCalled();
    expect(createTurnMock).not.toHaveBeenCalled();
  });

  it("创建任务失败时保留输入内容", async () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "workspace-1" });
    createTaskMock.mockResolvedValue(false);
    render();

    const input = getInput();
    act(() => {
      setInputValue(input, "不要丢");
    });

    await act(async () => {
      getSendButton().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(createTaskMock).toHaveBeenCalledWith("不要丢", "workspace-1");
    expect(input.value).toBe("不要丢");
  });
});
