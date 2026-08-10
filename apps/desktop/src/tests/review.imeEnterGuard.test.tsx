// @vitest-environment happy-dom
/**
 * 缺陷验证 #4：Enter 发送缺少 IME 组合态（isComposing）防护。
 *
 * 背景：InputBar 与 NewTaskPage 的 keyDown 处理器只判断
 * `e.key === "Enter" && !e.shiftKey`（NewTaskPage 甚至连 shiftKey 都未判），
 * 未判断 `e.nativeEvent.isComposing`。中文/日文等 IME 输入过程中按 Enter
 * 是「上屏候选词」，此时 isComposing=true，绝不应触发发送——否则用户组词
 * 过程中输入被提前发出。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { InputBar } from "@/components/layout/InputBar";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const taskMocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  createTurn: vi.fn(),
  cancelTurn: vi.fn(),
}));

// useTask 涉及 SSE / API 链路，整体替换为可控桩，仅观测发送回调是否被触发。
vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: taskMocks.createTask,
    createTurn: taskMocks.createTurn,
    cancelTurn: taskMocks.cancelTurn,
    operation: { loading: false, error: null },
  }),
}));
// AgentSelector 挂载即拉取 /agents，替换为空桩避免网络依赖。
vi.mock("@/components/chat/AgentSelector", () => ({ AgentSelector: () => null }));
// 工作区目录选择器依赖 Tauri 对话框，本测试不触达，替换为桩防御导入副作用。
vi.mock("@/services/workspace", () => ({ pickAndCreateWorkspace: vi.fn() }));
// 客户端 trace / 性能埋点走 logger→console，mock 掉保持输出干净。
vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(),
  endClientTrace: vi.fn(),
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));
vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

/** 构造并派发一个带 isComposing 标记的原生 keydown（React 经冒泡接收）。 */
function dispatchComposingEnter(target: Element, isComposing: boolean): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "Enter",
    isComposing,
    bubbles: true,
    cancelable: true,
  });
  fireEvent(target, event);
  return event;
}

describe("Enter 发送的 IME 组合态防护", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    taskMocks.createTask.mockResolvedValue(true);
    taskMocks.createTurn.mockResolvedValue(true);
    useTaskStore.setState({
      activeTaskId: "task-1",
      activeTurnId: null,
      selectedAgentId: "developer",
    } as never);
    useTurnStore.setState({ streamingTurnId: null } as never);
    useWorkspaceStore.setState({
      activeWorkspaceId: "ws-1",
      workspaces: [{ workspace_id: "ws-1", name: "demo", path: "/tmp/demo" }],
    } as never);
  });

  // 测试目的：InputBar 中 IME 组合态 Enter（isComposing=true）不应触发发送。
  // 可能发现的缺陷：handleKeyDown 未判 isComposing，中文输入法上屏 Enter 误发消息。
  it("InputBar：isComposing=true 的 Enter 不应调用发送回调", () => {
    render(<InputBar />);
    const input = screen.getByPlaceholderText("给 Agent 下达任务...");
    fireEvent.change(input, { target: { value: "nihao" } });

    const event = dispatchComposingEnter(input, true);
    // 环境健全性检查：事件必须真实携带 isComposing=true，否则本用例无效。
    expect(event.isComposing).toBe(true);

    // 正确行为：IME 组词上屏不发送。
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
    expect(taskMocks.createTask).not.toHaveBeenCalled();
  });

  // 测试目的：正向对照——InputBar 正常 Enter（非组合态）应触发发送（证明键盘链路接通）。
  // 可能发现的缺陷：无（此用例应 PASS；若失败说明发送链路未接通，主用例结论无效）。
  it("对照：InputBar 正常 Enter（isComposing=false）应调用 createTurn", () => {
    render(<InputBar />);
    const input = screen.getByPlaceholderText("给 Agent 下达任务...");
    fireEvent.change(input, { target: { value: "hello" } });

    dispatchComposingEnter(input, false);
    expect(taskMocks.createTurn).toHaveBeenCalledTimes(1);
  });

  // 测试目的：NewTaskPage 中 IME 组合态 Enter 不应触发首条消息创建。
  // 可能发现的缺陷：同 InputBar——keyDown 未判 isComposing，组词上屏误创建任务。
  it("NewTaskPage：isComposing=true 的 Enter 不应调用 createTask", () => {
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "nihao" } });

    const event = dispatchComposingEnter(input, true);
    expect(event.isComposing).toBe(true);

    expect(taskMocks.createTask).not.toHaveBeenCalled();
  });

  // 测试目的：正向对照——NewTaskPage 正常 Enter 应触发 createTask。
  // 可能发现的缺陷：无（此用例应 PASS）。
  it("对照：NewTaskPage 正常 Enter（isComposing=false）应调用 createTask", () => {
    render(<NewTaskPage onCreated={vi.fn()} />);
    const input = screen.getByPlaceholderText("描述这次任务...");
    fireEvent.change(input, { target: { value: "build something" } });

    dispatchComposingEnter(input, false);
    expect(taskMocks.createTask).toHaveBeenCalledTimes(1);
  });
});
