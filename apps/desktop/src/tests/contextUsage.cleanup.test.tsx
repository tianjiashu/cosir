// @vitest-environment happy-dom
/**
 * 上下文占用缓存的生命周期清理 + 删除任务 / 删除工作区时断开其残留 SSE 流。
 *
 * 守护不变量：
 * - 任务被删除 / 工作区任务被清空 / 会话被清空后，其占用条目随之清除，不留无归属
 *   条目（usageByTaskId 的规模始终受 tasksById 约束）。
 * - 删除任务会断开该任务全部 turn 的 SSE 连接；删除工作区会断开其下全部任务的
 *   连接。避免已删任务的残留流继续投递事件并在各 store 中重建条目。
 *
 * 可证伪性：旧实现是全局单值且 ``removeTask`` 完全不触及 usage store；同时删除路径
 * 只调 ``taskStore.removeTask``，没有任何断流动作。本文件全部用例在旧实现下均失败。
 *
 * @module tests/contextUsage.cleanup
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";

const rafCallbacks: FrameRequestCallback[] = [];
vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
});
vi.stubGlobal("cancelAnimationFrame", () => {});

const { FakeSSEConnection } = vi.hoisted(() => {
  class FakeSSEConnection {
    static instances: FakeSSEConnection[] = [];
    readonly taskId: string;
    readonly turnId: string;
    readonly onEvent: (event: RuntimeEvent) => void;
    disconnected = false;

    constructor(options: {
      taskId: string;
      turnId: string;
      onEvent: (event: RuntimeEvent) => void;
      onError?: (error: Error) => void;
      onStateChange?: (state: unknown) => void;
    }) {
      this.taskId = options.taskId;
      this.turnId = options.turnId;
      // 对齐真实 SSEConnection 的 disconnect 语义：abort 之后不再产出任何事件。
      this.onEvent = (event: RuntimeEvent) => {
        if (this.disconnected) {
          return;
        }
        options.onEvent(event);
      };
      FakeSSEConnection.instances.push(this);
    }

    /** 永不 resolve：模拟流保持打开，连接才不会被 useSSE 自行出池。 */
    connect(): Promise<void> {
      return new Promise(() => {});
    }

    disconnect(): void {
      this.disconnected = true;
    }
  }
  return { FakeSSEConnection };
});

vi.mock("@/services/sse", () => ({
  SSEConnection: FakeSSEConnection,
  SSEConnectionState: {
    IDLE: "idle",
    CONNECTING: "connecting",
    STREAMING: "streaming",
    CLOSED: "closed",
  },
}));

const apiMocks = vi.hoisted(() => ({
  deleteTask: vi.fn().mockResolvedValue(undefined),
  getTask: vi.fn(),
  listTaskTurns: vi.fn().mockResolvedValue([]),
  listTaskEvents: vi.fn().mockResolvedValue([]),
  createTask: vi.fn(),
  createTaskTurn: vi.fn(),
  cancelTurn: vi.fn(),
}));

vi.mock("@/services/api", () => apiMocks);

vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));
vi.mock("@/lib/perf", () => ({
  PerfTrace: { markCurrent: vi.fn(), endCurrent: vi.fn(), startCurrent: vi.fn() },
}));

import { useTask } from "@/hooks/useTask";
import { useSSE } from "@/hooks/useSSE";
import { useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";

/**
 * 模拟生产形态的两个独立 ``useTask()`` 实例。
 *
 * InputBar 负责建流、Sidebar 负责删除，二者各自实例化 useTask（因而各自实例化
 * useSSE）。这正是连接池必须是模块级单例的原因：只有共享池才能让 Sidebar 的删除
 * 真正断开 InputBar 建立的连接。
 */
function StreamOwner() {
  const { connect } = useSSE();
  (window as unknown as { __connect?: typeof connect }).__connect = connect;
  return null;
}

/** 暴露 useTask.deleteTask / disconnectTasks 的最小测试组件。 */
function Harness() {
  const { deleteTask, disconnectTasks } = useTask();
  (window as unknown as { __deleteTask?: typeof deleteTask }).__deleteTask = deleteTask;
  (window as unknown as { __disconnectTasks?: typeof disconnectTasks }).__disconnectTasks = disconnectTasks;
  return <StreamOwner />;
}

/** 取出 Harness 暴露的 deleteTask。 */
function deleteTaskHandle() {
  return (window as unknown as { __deleteTask: (taskId: number) => Promise<void> }).__deleteTask;
}

/** 取出 StreamOwner 暴露的 connect。 */
function connectHandle() {
  return (window as unknown as { __connect: (taskId: number, turnId: number) => Promise<void> })
    .__connect;
}

/** 取出 Harness 暴露的 disconnectTasks。 */
function deleteTasksHandle() {
  return (window as unknown as { __disconnectTasks: (taskIds: number[]) => number })
    .__disconnectTasks;
}

/** 驱动一次攒批 flush。 */
function flushFrame() {
  act(() => {
    const pending = rafCallbacks.splice(0, rafCallbacks.length);
    for (const cb of pending) {
      cb(0);
    }
  });
}

beforeEach(() => {
  cleanup();
  rafCallbacks.length = 0;
  FakeSSEConnection.instances = [];
  apiMocks.deleteTask.mockClear();
  useContextUsageStore.setState({ usageByTaskId: {} });
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    drafts: {},
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} } as never);
});

describe("上下文占用缓存的生命周期清理", () => {
  // removeTask 只清除该 task 的条目，其它 task 不受影响。
  it("removeTask 清除该 task 的占用，不影响其它 task", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");
    store.setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    useTaskStore.getState().removeTask(1);

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]).toBeUndefined();
    expect(state.usageByTaskId[2]?.usedTokens).toBe(5000);
  });

  // clearWorkspaceTasks 清除该工作区全部任务的条目，其它工作区不受影响。
  it("clearWorkspaceTasks 清除该工作区全部任务的占用", () => {
    // 待清理集合由 store 中的任务实体推导，故必须先播种任务记录。
    act(() => {
      useTaskStore.setState({
        tasksById: {
          1: { task_id: 1, workspace_id: 1 } as never,
          2: { task_id: 2, workspace_id: 1 } as never,
          3: { task_id: 3, workspace_id: 2 } as never,
        },
      });
    });
    const store = useContextUsageStore.getState();
    store.setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");
    store.setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");
    store.setUsage(3, { used_tokens: 7000, total_tokens: 128000 }, "2026-08-28T10:00:02Z");

    useTaskStore.getState().clearWorkspaceTasks(1);

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]).toBeUndefined();
    expect(state.usageByTaskId[2]).toBeUndefined();
    expect(state.usageByTaskId[3]?.usedTokens).toBe(7000);
  });

  // clearTasks（清空会话）清空全部条目。
  it("clearTasks 清空全部 task 的占用", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");
    store.setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");
    // 前置断言：否则「清空后为空」在旧实现（全局单值、无 usageByTaskId）下也会
    // 因为该字段恒为 {} 而假通过，测试失去证伪能力。
    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeDefined();
    expect(useContextUsageStore.getState().usageByTaskId[2]).toBeDefined();

    useTaskStore.getState().clearTasks();

    expect(useContextUsageStore.getState().usageByTaskId).toEqual({});
  });
});

describe("删除任务时断开其残留 SSE 流", () => {
  // 核心：连接由「另一个 useTask/useSSE 实例」建立，删除方仍必须能断掉它。
  // 这是连接池必须为模块级单例的判据——池若随 hook 实例分裂，本用例必然失败。
  it("deleteTask 能断开由另一个 useSSE 实例建立的连接", async () => {
    render(<Harness />);

    // 模拟 InputBar 建流（独立的 useSSE 实例）。
    await act(async () => {
      await connectHandle()(1, 101);
      await connectHandle()(1, 102);
      await connectHandle()(2, 202);
    });

    // 模拟 Sidebar 删除任务 1（另一个 useTask 实例）。
    await act(async () => {
      await deleteTaskHandle()(1);
    });

    const connA1 = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const connA2 = FakeSSEConnection.instances.find((c) => c.turnId === "102")!;
    const connB = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;
    expect(connA1.disconnected).toBe(true);
    expect(connA2.disconnected).toBe(true);
    expect(connB.disconnected).toBe(false);
  });

  // 删除任务后，其占用条目与后端调用顺序正确（先断流、再删后端、再清缓存）。
  it("deleteTask 清缓存并调后端删除", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");
    useContextUsageStore.getState().setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    await act(async () => {
      await deleteTaskHandle()(1);
    });

    expect(apiMocks.deleteTask).toHaveBeenCalledWith("1");
    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeUndefined();
    expect(useContextUsageStore.getState().usageByTaskId[2]?.usedTokens).toBe(5000);
  });

  // 删除后残留流即便再投递事件也不得重建条目（连接已 abort，不再产出事件）。
  it("删除任务后残留流不得重建该 task 的占用条目", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });
    useContextUsageStore.getState().setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    await act(async () => {
      await deleteTaskHandle()(1);
    });

    // 模拟残留流在删除后仍投递了一帧事件。
    const stale = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    act(() => {
      stale.onEvent({
        event_id: "stale-1",
        event_type: "context_usage",
        task_id: 1,
        turn_id: 101,
        sequence: 9,
        created_at: "2026-08-28T10:00:09Z",
        payload: { used_tokens: 123456, total_tokens: 200000 },
      } as RuntimeEvent);
    });
    flushFrame();

    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeUndefined();
    expect(useContextUsageStore.getState().usageByTaskId[2]?.usedTokens).toBe(5000);
  });

  // 删除工作区：其下**全部**任务的连接都要断开，一个都不能留。
  // 该路径与单任务删除不同源（走 api.deleteWorkspace + clearWorkspaceTasks），
  // 若漏了断流，工作区下正在流式的任务会全部泄漏。
  it("disconnectTasks 断开一组 task 的连接，不影响组外 task", async () => {
    render(<Harness />);

    await act(async () => {
      await connectHandle()(1, 101);
      await connectHandle()(2, 202);
      await connectHandle()(3, 303);
    });

    act(() => {
      expect(deleteTasksHandle()([1, 2])).toBe(2);
    });

    const conn1 = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const conn2 = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;
    const conn3 = FakeSSEConnection.instances.find((c) => c.turnId === "303")!;
    expect(conn1.disconnected).toBe(true);
    expect(conn2.disconnected).toBe(true);
    expect(conn3.disconnected).toBe(false);
  });

  // 工作区删除后，其残留流不得重建任何已删任务的占用条目。
  it("工作区删除后残留流不得重建已删 task 的占用条目", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });
    useContextUsageStore.getState().setUsage(9, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    act(() => {
      deleteTasksHandle()([1]);
    });

    // 模拟残留流在删除后仍投递了一帧事件。
    const stale = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    act(() => {
      stale.onEvent({
        event_id: "stale-ws-1",
        event_type: "context_usage",
        task_id: 1,
        turn_id: 101,
        sequence: 9,
        created_at: "2026-08-28T10:00:09Z",
        payload: { used_tokens: 123456, total_tokens: 200000 },
      } as RuntimeEvent);
    });
    flushFrame();

    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeUndefined();
    // 组外 task 的占用不受影响。
    expect(useContextUsageStore.getState().usageByTaskId[9]?.usedTokens).toBe(5000);
  });

  // 阻断级回归：工作区「从未懒加载」时 tasksByWorkspaceId 为空，但任务实体在
  // tasksById 里且正在流式。此时取 id 必须仍能覆盖到它，否则断流静默失效。
  it("工作区分组未加载时，getWorkspaceTaskIds 仍能覆盖 tasksById 中的任务", () => {
    // 只播种实体缓存，分组缓存刻意留空——模拟用户从未展开该工作区。
    act(() => {
      useTaskStore.setState({
        tasksByWorkspaceId: {},
        tasksById: {
          7: { task_id: 7, workspace_id: 5 } as never,
          8: { task_id: 8, workspace_id: 6 } as never,
        },
      });
    });

    expect(useTaskStore.getState().getWorkspaceTaskIds(5)).toEqual([7]);
    // 另一个工作区的任务不得被卷入。
    expect(useTaskStore.getState().getWorkspaceTaskIds(6)).toEqual([8]);
  });

  // 阻断级回归：分组与实体两路 id 都取，合并去重，不遗漏任一来源。
  it("getWorkspaceTaskIds 合并分组与实体两路并去重", () => {
    act(() => {
      useTaskStore.setState({
        tasksByWorkspaceId: { 5: [{ task_id: 7 } as never, { task_id: 9 } as never] },
        tasksById: {
          7: { task_id: 7, workspace_id: 5 } as never,
          9: { task_id: 9, workspace_id: 5 } as never,
          10: { task_id: 10, workspace_id: 5 } as never,
        },
      });
    });

    const ids = useTaskStore.getState().getWorkspaceTaskIds(5);
    expect(ids.sort((a, b) => a - b)).toEqual([7, 9, 10]);
  });
});
