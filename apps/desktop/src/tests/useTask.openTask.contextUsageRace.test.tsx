// @vitest-environment happy-dom
/**
 * openTask 的上下文占用回填竞态。
 *
 * 守护不变量：用户快速从任务 A 切到任务 B 时，A 的迟到 ``GET /tasks/{id}`` 响应
 * 不得让 B 的圆环显示 A 的占用；同时正在流式的任务不得被稍旧的持久化值回拨（D6）。
 *
 * 真实使用 useTask / contextUsageStore / taskStore，仅 mock 网络层（``@/services/api``）
 * 与 SSE 底层（``@/services/sse``），由测试完全掌控响应到达顺序。
 *
 * 可证伪性：旧实现在 ``seq`` 竞态判断**之前**无条件执行全局 ``setUsage`` / ``reset``。
 * 本文件核心用例在旧实现下必然失败——A 的迟到响应会覆盖全局占用，圆环显示 A 的值；
 * 且 ``reset()`` 会让 B 的圆环归零。
 *
 * @module tests/useTask.openTask.contextUsageRace
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import type { TaskRecord } from "@shared/task";

// 与 useSSE.contextUsageIsolation 同构：手动驱动 rAF 攒批。
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

    constructor(options: {
      taskId: string;
      turnId: string;
      onEvent: (event: unknown) => void;
      onError?: (error: Error) => void;
      onStateChange?: (state: unknown) => void;
    }) {
      this.taskId = options.taskId;
      this.turnId = options.turnId;
      this.onEvent = options.onEvent;
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

// 网络层由测试掌控：openTask 的 GET /tasks/{id} 与 GET /turns 全部可延迟兑现。
// useTask 以 `import * as api` 方式引用本模块，故 mock 必须给出具名导出（而非包一层
// `api` 对象），否则 api.getTask 取到的未定义成员会抛「No export is defined」。
const apiMocks = vi.hoisted(() => {
  const getTaskResolvers = new Map<string, (task: unknown) => void>();
  return {
    getTask: vi.fn((taskId: string) => new Promise((r) => getTaskResolvers.set(taskId, r))),
    listTaskTurns: vi.fn().mockResolvedValue([]),
    listTaskEvents: vi.fn().mockResolvedValue([]),
    createTask: vi.fn(),
    createTaskTurn: vi.fn(),
    deleteTask: vi.fn().mockResolvedValue(undefined),
    cancelTurn: vi.fn(),
    /** 兑现此前对 taskId 发起的 getTask 请求。 */
    resolveTask: (taskId: string, task: unknown) => {
      const resolve = getTaskResolvers.get(taskId);
      if (!resolve) {
        throw new Error(`未预期地请求了 task ${taskId}`);
      }
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

/** 暴露 useTask.openTask 并渲染真实圆环（圆环是用户可见的缺陷现场）。 */
function Harness() {
  const { openTask } = useTask();
  (window as unknown as { __openTask?: typeof openTask }).__openTask = openTask;
  return <ContextUsageRing />;
}

/** 取出 Harness 暴露的 openTask。 */
function openTaskHandle() {
  return (window as unknown as { __openTask: (taskId: number) => Promise<void> }).__openTask;
}

/**
 * 发起 openTask 并停在 api.getTask 上（in-flight，尚未产生任何副作用）。
 *
 * 必须分两步：``await act(async () => openTask(id))`` 会一直挂到该请求被兑现，而
 * 兑现动作又写在其后的 act 里，二者互相等待形成死锁。先在同步 act 中取出 Promise，
 * 之后再单独 act 兑现并 await，顺序才可控。
 *
 * @returns openTask 的 Promise，需在兑现其 getTask 后 await。
 */
function startOpenTask(taskId: number): Promise<void> {
  let pending: Promise<void> | undefined;
  act(() => {
    pending = openTaskHandle()(taskId);
  });
  return pending!;
}

/** 构造后端任务记录。 */
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
  apiMocks.listTaskTurns.mockClear();
  apiMocks.listTaskTurns.mockResolvedValue([]);
  apiMocks.listTaskEvents.mockClear();
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

describe("openTask 占用回填的竞态防护", () => {
  // 核心回归（对应缺陷 D3）：A 的迟到响应到达后，圆环仍显示当前活跃任务 B 的占用。
  it("A 的迟到响应不覆盖当前活跃任务 B 的圆环", async () => {
    render(<Harness />);
    const openTask = openTaskHandle();

    // A、B 同时处于 in-flight：两次 openTask 都停在 api.getTask 上，未产生任何副作用。
    const openedA = startOpenTask(1);
    const openedB = startOpenTask(2);

    // B 先返回并成为活跃任务；A 的响应迟到。
    await act(async () => {
      apiMocks.resolveTask("2", taskRecord(2, 5000, 128000, "2026-08-28T10:00:01Z"));
      await openedB;
    });
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, 1000, 200000, "2026-08-28T10:00:00Z"));
      await openedA;
    });

    expect(useTaskStore.getState().activeTaskId).toBe(2);
    // 圆环显示 B 的占用比例（5000/128000 ≈ 3.9%），而非 A 的（1000/200000 = 0.5%）。
    expect(useContextUsageStore.getState().usageByTaskId[2]?.usedTokens).toBe(5000);
    expect(document.body.textContent).toContain("3.9%");
    expect(document.body.textContent).not.toContain("0.5%");
  });

  // 迟到响应写入的是它自己 task 的条目，不污染当前任务（store 层面）。
  it("迟到响应只写入其自身 task 的缓存条目", async () => {
    render(<Harness />);

    const openedB = startOpenTask(2);
    await act(async () => {
      apiMocks.resolveTask("2", taskRecord(2, 5000, 128000, "2026-08-28T10:00:01Z"));
      await openedB;
    });

    // A 的响应此刻才到达（activeTaskId 已是 B）。
    const openedA = startOpenTask(1);
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, 1000, 200000, "2026-08-28T10:00:00Z"));
      await openedA;
    });

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]?.usedTokens).toBe(1000);
    expect(state.usageByTaskId[2]?.usedTokens).toBe(5000);
  });

  // 单调守卫（对应缺陷 D6）：运行中任务不被更早的持久化值回拨。
  it("运行中任务不被更早的持久化值回拨", async () => {
    useContextUsageStore
      .getState()
      .setUsage(1, { used_tokens: 8000, total_tokens: 200000 }, "2026-08-28T10:00:05Z");

    render(<Harness />);

    const opened = startOpenTask(1);
    // 后端返回的是上一轮写入时刻（早于本地实时值）。
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, 1000, 200000, "2026-08-28T10:00:00Z"));
      await opened;
    });

    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(8000);
  });

  // 后端无占用数据时清除该 task 缓存，而不是全局 reset。
  it("后端无占用数据时只清除该 task，不影响其它 task", async () => {
    useContextUsageStore
      .getState()
      .setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    render(<Harness />);

    const opened = startOpenTask(1);
    await act(async () => {
      apiMocks.resolveTask("1", taskRecord(1, null, null, "2026-08-28T10:00:00Z"));
      await opened;
    });

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]).toBeUndefined();
    expect(state.usageByTaskId[2]?.usedTokens).toBe(5000);
  });
});
