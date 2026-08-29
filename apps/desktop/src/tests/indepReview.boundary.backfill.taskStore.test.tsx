// @vitest-environment happy-dom
/**
 * 独立审查：_backfillContextUsage 单调守卫边界 + taskStore 删除路径误伤边界。
 *
 * 攻击面（开发方 26 用例未覆盖）：
 *  - 单调守卫时钟相等边界：local.updatedAt === task.updated_at 时是否回拨（应为「不回拨」）
 *  - openTask 回填：后端两字段其一为 null 时清除该 task（而非全局 reset）
 *  - removeTask 删除「不存在的 task」不得误伤其它 task 的 usage / 草稿
 *  - clearWorkspaceTasks 删除不存在的 workspace 不得抛错、不得误伤
 *
 * @module tests/indepReview.boundary.backfill.taskStore
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import type { TaskRecord } from "@shared/task";

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
    readonly onEvent: (event: unknown) => void;
    disconnected = false;
    constructor(options: { taskId: string; turnId: string; onEvent: (event: unknown) => void; onError?: (e: Error) => void; onStateChange?: (s: unknown) => void }) {
      this.taskId = options.taskId;
      this.turnId = options.turnId;
      this.onEvent = (event: unknown) => {
        if (this.disconnected) return;
        options.onEvent(event);
      };
      FakeSSEConnection.instances.push(this);
    }
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
  SSEConnectionState: { IDLE: "idle", CONNECTING: "connecting", STREAMING: "streaming", CLOSED: "closed" },
}));
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

const apiMocks = vi.hoisted(() => {
  const getTaskResolvers = new Map<string, (task: unknown) => void>();
  return {
    getTask: vi.fn((taskId: string) => new Promise((r) => getTaskResolvers.set(taskId, r))),
    listTaskTurns: vi.fn().mockResolvedValue([]),
    listTaskEvents: vi.fn().mockResolvedValue([]),
    resolveTask: (taskId: string, task: unknown) => {
      const resolve = getTaskResolvers.get(taskId);
      if (!resolve) throw new Error(`未预期地请求了 task ${taskId}`);
      getTaskResolvers.delete(taskId);
      resolve(task);
    },
  };
});
vi.mock("@/services/api", () => apiMocks);

import { useTask } from "@/hooks/useTask";
import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";

function Harness() {
  const { openTask } = useTask();
  (window as unknown as { __openTask?: typeof openTask }).__openTask = openTask;
  return <ContextUsageRing />;
}
function openTaskHandle() {
  return (window as unknown as { __openTask: (id: number) => Promise<void> }).__openTask;
}
function startOpenTask(taskId: number): Promise<void> {
  let pending: Promise<void> | undefined;
  act(() => {
    pending = openTaskHandle()(taskId);
  });
  return pending!;
}
function taskRecord(taskId: number, used: number | null, total: number | null, updatedAt: string): TaskRecord {
  return {
    task_id: taskId,
    workspace_id: 1,
    title: `task-${taskId}`,
    created_at: "2026-08-28T09:00:00Z",
    updated_at: updatedAt,
    execution_status: "idle",
    is_temporary: false,
    context_usage_used: used,
    context_window_total: total,
  } as TaskRecord;
}

beforeEach(() => {
  cleanup();
  rafCallbacks.length = 0;
  FakeSSEConnection.instances = [];
  apiMocks.getTask.mockClear();
  apiMocks.listTaskTurns.mockResolvedValue([]);
  apiMocks.listTaskEvents.mockResolvedValue([]);
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

describe("_backfillContextUsage 单调守卫的时钟相等边界", () => {
  // 本地实时值 updatedAt 与后端 updated_at「完全相等」时，不得回拨（守卫应为 >= 跳过）。
  it("本地与后端 updatedAt 相等时不回拨", async () => {
    const sameTs = "2026-08-28T10:00:05Z";
    useContextUsageStore
      .getState()
      .setUsage(1, { used_tokens: 8000, total_tokens: 200000 }, sameTs);

    render(<Harness />);
    const opened = startOpenTask(1);
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, 1000, 200000, sameTs));
      await opened;
    });

    // 相等不应回拨到 1000。
    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(8000);
  });

  // 本地较旧（updatedAt 早于后端）时应被回填为新值（守卫放行）。
  it("本地较旧时被后端新值回填", async () => {
    useContextUsageStore
      .getState()
      .setUsage(1, { used_tokens: 8000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");

    render(<Harness />);
    const opened = startOpenTask(1);
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, 1000, 200000, "2026-08-28T10:00:05Z"));
      await opened;
    });

    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(1000);
  });
});

describe("removeTask 删除不存在的 task 的误伤边界", () => {
  // 删除一个根本不存在的 taskId：不得误删其它 task 的 usage、不得抛错。
  it("removeTask 不存在的 task 不误伤其它 task 的占用与草稿", () => {
    useContextUsageStore.getState().setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "t");
    act(() => {
      useTaskStore.setState({
        tasksById: { 2: { task_id: 2, workspace_id: 1 } as never },
        drafts: { 2: "draft-2" } as never,
      });
    });

    expect(() => act(() => useTaskStore.getState().removeTask(9999))).not.toThrow();

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[2]?.usedTokens).toBe(5000);
    // 不存在的 task 删除不应改动 task 2 的草稿。
    expect(useTaskStore.getState().drafts[2]).toBe("draft-2");
    expect(useTaskStore.getState().tasksById[2]).toBeDefined();
  });

  // removeTask 删除正在被作为 activeTask 的 task 时，必须同步清空 activeTaskId。
  it("removeTask 删除当前活跃 task 时清空 activeTaskId", () => {
    act(() => {
      useTaskStore.setState({
        tasksById: { 1: { task_id: 1, workspace_id: 1 } as never },
        activeTaskId: 1,
        activeTurnId: 5,
      });
    });
    act(() => useTaskStore.getState().removeTask(1));
    expect(useTaskStore.getState().activeTaskId).toBeNull();
    expect(useTaskStore.getState().activeTurnId).toBeNull();
  });
});

describe("clearWorkspaceTasks 删除不存在的 workspace", () => {
  // 清空一个不存在的 workspace：不得抛错、不得误伤 task 1（属于 workspace 1）。
  it("clearWorkspaceTasks 不存在的 workspace 不误伤其它 workspace 的 task", () => {
    act(() => {
      useTaskStore.setState({
        tasksById: { 1: { task_id: 1, workspace_id: 1 } as never },
        tasksByWorkspaceId: { 1: [{ task_id: 1, workspace_id: 1 } as never] },
        loadedWorkspaceIds: new Set([1]),
      });
    });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 5000, total_tokens: 128000 }, "t");

    expect(() => act(() => useTaskStore.getState().clearWorkspaceTasks(9999))).not.toThrow();

    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(5000);
    expect(useTaskStore.getState().tasksById[1]).toBeDefined();
  });
});
