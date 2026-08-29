// @vitest-environment happy-dom
/**
 * useTask.createTurn 透传 reasoning_effort 的集成测试（设计 §推理性强度选择）。
 *
 * 守护不变量：
 * 1. createTurn 构造的 createTaskTurn 请求体携带 reasoning_effort 字段；
 * 2. 当 useTaskStore.selectedReasoningEffort 为非空档位时透传该档位名；
 * 3. 当 selectedReasoningEffort 为 null（未指定）时不传 reasoning_effort（等同 undefined，
 *    同后端 None=max/不指定语义）。
 *
 * 真实使用 useTask（非 mock），仅 mock 底层外部副作用（api 调用、SSE、trace/perf），
 * turnStore/eventStore/taskStore 均为真实 zustand 实例，验证端到端请求体构造。
 *
 * @module tests/useTask.reasoningEffort
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/react";

// ---- 捕获 createTaskTurn 调用参数的 mock ----
const createTaskTurnMock = vi.hoisted(() => vi.fn());

vi.mock("@/services/api", () => ({
  listModels: vi.fn().mockResolvedValue([]),
  listProviders: vi.fn().mockResolvedValue([]),
  listTaskTurns: vi.fn().mockResolvedValue([]),
  createTask: vi.fn().mockResolvedValue({
    task_id: "real-task",
    workspace_id: "ws-1",
    agent_id: "main_agent",
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }),
  createTaskTurn: (taskId: string, request: unknown) => {
    createTaskTurnMock(taskId, request);
    return Promise.resolve({
      turn_id: "real-turn",
      task_id,
      input_text: "",
      status: "pending",
      end_reason: null,
      response_text: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  },
}));
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));
vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({ connect: vi.fn().mockResolvedValue(undefined), disconnectTurn: vi.fn() }),
}));
vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(),
  endClientTrace: vi.fn(),
  hasClientTrace: () => false,
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "t", mark: vi.fn() })),
  },
}));
vi.mock("@/hooks/useWorkspaceTaskLazyLoad", () => ({
  loadWorkspaceTasks: vi.fn().mockResolvedValue(undefined),
}));

import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";

/** 调用 useTask().createTurn 的最小测试组件。 */
function Harness() {
  const { createTurn } = useTask();
  // 暴露到 window 以便测试驱动（避免额外 DOM 交互）。
  (window as unknown as { __createTurn?: typeof createTurn }).__createTurn = createTurn;
  return null;
}

function resetStores() {
  cleanup();
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    selectedModelName: null,
    selectedReasoningEffort: null,
    availableModels: [],
    modelsLoaded: true,
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
  createTaskTurnMock.mockReset();
}

beforeEach(() => {
  resetStores();
});

describe("createTurn 透传 reasoning_effort", () => {
  it("selectedReasoningEffort 非空时透传该档位名", async () => {
    useTaskStore.setState({
      activeTaskId: "t-1",
      selectedModelName: "deepseek/deepseek-v4-flash",
      selectedReasoningEffort: "high",
    });
    render(<Harness />);
    const createTurn = (window as unknown as { __createTurn: typeof createTurn } & Record<
      string,
      unknown
    >).__createTurn;
    await act(async () => {
      await createTurn("hello");
    });
    await waitFor(() => expect(createTaskTurnMock).toHaveBeenCalledTimes(1));
    const [, request] = createTaskTurnMock.mock.calls[0] as [string, { reasoning_effort?: string }];
    expect(request.reasoning_effort).toBe("high");
  });

  it("selectedReasoningEffort 为 null 时不传 reasoning_effort（等同未指定）", async () => {
    useTaskStore.setState({
      activeTaskId: "t-1",
      selectedModelName: "deepseek/deepseek-v4-flash",
      selectedReasoningEffort: null,
    });
    render(<Harness />);
    const createTurn = (window as unknown as { __createTurn: typeof createTurn } & Record<
      string,
      unknown
    >).__createTurn;
    await act(async () => {
      await createTurn("hello");
    });
    await waitFor(() => expect(createTaskTurnMock).toHaveBeenCalledTimes(1));
    const [, request] = createTaskTurnMock.mock.calls[0] as [string, { reasoning_effort?: string }];
    expect(request.reasoning_effort).toBeUndefined();
  });
});
