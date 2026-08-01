// @vitest-environment happy-dom

import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTask } from "@/hooks/useTask";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useEventStore } from "@/stores/eventStore";
import { makeTask } from "@/tests/test-utils/factories";
import * as api from "@/services/api";
import type { TurnRecord } from "@shared/turn";
import type { RuntimeEvent } from "@shared/events";

const hookMocks = vi.hoisted(() => ({
  connect: vi.fn().mockResolvedValue(undefined),
  disconnect: vi.fn(),
  logError: vi.fn(),
}));

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    connect: hookMocks.connect,
    disconnect: hookMocks.disconnect,
  }),
}));

vi.mock("@/lib/logger", () => ({
  logError: (...args: unknown[]) => hookMocks.logError(...args),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logDebug: vi.fn(),
}));

vi.mock("@/services/api", () => ({
  createTask: vi.fn(),
  createTaskTurn: vi.fn(),
  listTaskTurns: vi.fn(),
  listTaskEvents: vi.fn(),
  getTask: vi.fn(),
  cancelTurn: vi.fn(),
}));

type UseTaskValue = ReturnType<typeof useTask>;

/**
 * 创建一个外部可手动 resolve 的 Promise，用于精确控制异步阶段推进顺序。
 *
 * @typeParam T - Promise 兑现值类型。
 * @returns 含 promise 与 resolve 句柄的 deferred 对象。
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let container: HTMLDivElement;
let root: Root;
let currentHook: UseTaskValue;

function HookHarness(): null {
  const task = useTask();

  useEffect(() => {
    currentHook = task;
  }, [task]);

  return null;
}

function renderHookHarness(): void {
  act(() => {
    root.render(<HookHarness />);
  });
}

function makeTurn(turnId: string, overrides: Partial<TurnRecord> = {}): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: "继续",
    status: "pending",
    end_reason: null,
    response_text: null,
    agent_id: "developer",
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

describe("useTask", () => {
  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    useTaskStore.getState().clearTasks();
    useTaskStore.getState().setSelectedAgentId("developer");
    useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });

    vi.mocked(api.createTask).mockReset();
    vi.mocked(api.createTaskTurn).mockReset();
    vi.mocked(api.listTaskTurns).mockReset();
    hookMocks.connect.mockReset().mockResolvedValue(undefined);
    hookMocks.disconnect.mockReset();
    hookMocks.logError.mockReset();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
  });

  it("追加 turn 时使用最新选中的 Agent", async () => {
    vi.mocked(api.createTaskTurn).mockResolvedValue(makeTurn("turn-2", { agent_id: "reviewer" }));
    useTaskStore.getState().addTask(makeTask("task-1", { latest_turn_id: "turn-1" }));

    renderHookHarness();

    act(() => {
      useTaskStore.getState().setSelectedAgentId("reviewer");
    });

    await act(async () => {
      await currentHook.createTurn("继续");
    });

    expect(api.createTaskTurn).toHaveBeenCalledWith("task-1", {
      input_text: "继续",
      agent_id: "reviewer",
    });
  });

  it("并发创建失败时只清理当前失败调用创建的临时任务", async () => {
    const nowSpy = vi.spyOn(Date, "now");
    nowSpy.mockReturnValueOnce(1).mockReturnValueOnce(2);

    let rejectFirst!: (reason?: unknown) => void;
    vi.mocked(api.createTask)
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectFirst = reject;
          }),
      )
      .mockImplementationOnce(() => new Promise(() => undefined));

    renderHookHarness();

    let firstResult!: Promise<boolean>;
    act(() => {
      firstResult = currentHook.createTask("first", "workspace-1");
      void currentHook.createTask("second", "workspace-1");
    });

    await act(async () => {
      rejectFirst(new Error("boom"));
      await firstResult;
    });

    const taskIds = useTaskStore.getState().tasks.map((task) => task.task_id);
    expect(taskIds).not.toContain("temp-1");
    expect(taskIds).toContain("temp-2");
  });

  it("取消 turn 后等待 SSE 收到 run_cancelled，不主动断开流", async () => {
    const cancelledTurn = makeTurn("turn-1", { status: "cancelled" });
    vi.mocked(api.cancelTurn).mockResolvedValue(cancelledTurn);
    useTaskStore.getState().addTask(makeTask("task-1", { latest_turn_id: "turn-1" }));
    useTaskStore.getState().setActiveTask("task-1", "turn-1");
    useTurnStore.getState().setTurnsForTask("task-1", [makeTurn("turn-1", { status: "running" })]);
    useTurnStore.getState().setStreamingTurn("turn-1");

    renderHookHarness();

    await act(async () => {
      await currentHook.cancelTurn();
    });

    expect(api.cancelTurn).toHaveBeenCalledWith("turn-1", "task-1");
    expect(hookMocks.disconnect).not.toHaveBeenCalled();
    expect(useTurnStore.getState().streamingTurnId).toBe("turn-1");
    expect(useTurnStore.getState().turnsByTaskId["task-1"][0].status).toBe("cancelled");
  });

  it("openTask 先渲染骨架再异步回填历史事件，不等 events 到达", async () => {
    const task = makeTask("task-1", { latest_turn_id: "turn-1" });
    const turns = [makeTurn("turn-1", { status: "completed" })];
    // 用显式 deferred 控制两阶段解析顺序，避免依赖 microtask 调度顺序的脆弱断言。
    const skeletonReady = deferred<void>();
    const eventsReady = deferred<RuntimeEvent[]>();
    vi.mocked(api.getTask).mockResolvedValue(task);
    vi.mocked(api.listTaskTurns).mockResolvedValue(turns);
    vi.mocked(api.listTaskEvents).mockImplementation(() => eventsReady.promise);
    // getTask/listTaskTurns 解析后让出骨架阶段。
    vi.mocked(api.getTask).mockImplementationOnce(async () => {
      const result = await Promise.resolve(task);
      skeletonReady.resolve();
      return result;
    });

    renderHookHarness();

    let openDone!: Promise<void>;
    await act(async () => {
      openDone = currentHook.openTask("task-1");
      await skeletonReady.promise;
    });

    // events 尚未返回时，活跃任务已切换（面板可渲染用户消息与 turn 骨架）。
    expect(useTaskStore.getState().activeTaskId).toBe("task-1");
    expect(useTurnStore.getState().turnsByTaskId["task-1"]).toHaveLength(1);
    expect(useEventStore.getState().eventsByTaskId["task-1"]).toBeUndefined();

    // events 到达后增量灌入。
    await act(async () => {
      eventsReady.resolve([]);
      await openDone;
    });
    expect(useEventStore.getState().eventsByTaskId["task-1"]).toEqual([]);
  });

  it("openTask 历史事件回填失败时只标记 eventsError，不回退已渲染骨架", async () => {
    const task = makeTask("task-1", { latest_turn_id: "turn-1" });
    const turns = [makeTurn("turn-1", { status: "completed" })];
    const skeletonReady = deferred<void>();
    vi.mocked(api.getTask).mockResolvedValue(task);
    vi.mocked(api.listTaskTurns).mockResolvedValue(turns);
    vi.mocked(api.listTaskEvents).mockRejectedValue(new Error("events boom"));
    vi.mocked(api.getTask).mockImplementationOnce(async () => {
      const result = await Promise.resolve(task);
      skeletonReady.resolve();
      return result;
    });

    renderHookHarness();

    let openDone!: Promise<void>;
    await act(async () => {
      openDone = currentHook.openTask("task-1");
      await skeletonReady.promise;
    });

    // 骨架已就绪，任务已切换，loading 不为失败态。
    expect(useTaskStore.getState().activeTaskId).toBe("task-1");
    expect(currentHook.operation.loading).toBe(false);
    expect(currentHook.operation.error).toBeNull();

    await act(async () => {
      await openDone;
    });
    // 回填失败仅记入 eventsError，骨架保持可用，并产出错误日志。
    expect(currentHook.operation.eventsError).toBe("events boom");
    expect(currentHook.operation.error).toBeNull();
    expect(currentHook.operation.loading).toBe(false);
    expect(hookMocks.logError).toHaveBeenCalledWith(
      "openTask 历史事件回填失败",
      expect.any(Error),
      expect.objectContaining({ task_id: "task-1" }),
    );
  });
});
