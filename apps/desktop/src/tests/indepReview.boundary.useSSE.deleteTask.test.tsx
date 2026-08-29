// @vitest-environment happy-dom
/**
 * 独立审查：useSSE / useTask.deleteTask / disconnectTask 的边界与缺陷挖掘。
 *
 * 攻击面（开发方 26 用例未覆盖）：
 *  - disconnectTask 对不存在的 taskId（no-op）
 *  - disconnectTask / removeByTask 对 temp- 前缀乐观轮次键的归属判断
 *  - deleteTask 后端 reject 时本地缓存与 store 是否保持（AC11）
 *  - 快速连续 deleteTask 同一 task（并发）
 *  - 同一 flush 批次内「同一 task 的多个 turn」交错时取哪条
 *  - 同一批次内同一 task 多 turn 交错，且存在其它 task 事件时各自独立
 *
 * @module tests/indepReview.boundary.useSSE.deleteTask
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

// 真实连接池单例的测试用替身：直接 import 生产模块（模块级单例）。
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
      this.onEvent = (event: RuntimeEvent) => {
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

const apiMocks = vi.hoisted(() => ({
  deleteTask: vi.fn().mockResolvedValue(undefined),
  getTask: vi.fn(),
  listTaskTurns: vi.fn().mockResolvedValue([]),
  listTaskEvents: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/services/api", () => apiMocks);

import { useTask } from "@/hooks/useTask";
import { useSSE } from "@/hooks/useSSE";
import { sseConnectionPool } from "@/services/sseConnectionPool";
import { useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";

function Harness() {
  const { connect, disconnectTask } = useSSE();
  const { deleteTask } = useTask();
  const w = window as unknown as { __connect?: typeof connect; __disconnectTask?: typeof disconnectTask; __deleteTask?: typeof deleteTask };
  w.__connect = connect;
  w.__disconnectTask = disconnectTask;
  w.__deleteTask = deleteTask;
  return null;
}

function connectHandle() {
  return (window as unknown as { __connect: (a: number, b: number) => Promise<void> }).__connect;
}
function disconnectTaskHandle() {
  return (window as unknown as { __disconnectTask: (a: number) => void }).__disconnectTask;
}
function deleteTaskHandle() {
  return (window as unknown as { __deleteTask: (a: number) => Promise<void> }).__deleteTask;
}

function flushFrame() {
  act(() => {
    const pending = rafCallbacks.splice(0, rafCallbacks.length);
    for (const cb of pending) cb(0);
  });
}

function usageEvent(taskId: number, turnId: number, used: number, total: number, seq = 1): RuntimeEvent {
  return {
    event_id: `evt-${taskId}-${turnId}-${used}`,
    event_type: "context_usage",
    task_id: taskId,
    turn_id: turnId,
    sequence: seq,
    created_at: "2026-08-28T10:00:00Z",
    payload: { used_tokens: used, total_tokens: total },
  } as RuntimeEvent;
}

beforeEach(() => {
  cleanup();
  rafCallbacks.length = 0;
  FakeSSEConnection.instances = [];
  apiMocks.deleteTask.mockReset().mockResolvedValue(undefined);
  // 清空模块级单例池的全部连接，避免用例间串味（键为 turnKey 形态，无法枚举预测）。
  sseConnectionPool.removeAll();
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

describe("disconnectTask 边界", () => {
  // 对不存在的 taskId（无任何连接）必须静默 no-op，不抛错、不影响其它连接。
  it("disconnectTask 不存在的 task 时静默 no-op", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });
    expect(() => act(() => disconnectTaskHandle()(9999))).not.toThrow();
    const conn = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    expect(conn.disconnected).toBe(false);
  });

  // 单测连接池 removeByTask：temp- 前缀的乐观轮次若 taskId 未登记为对应 task，
  // 不得被误断开（验证 removeByTask 按 taskId 精确匹配，而非按字符串前缀）。
  it("removeByTask 不误伤归属不同 task 的 temp- 键连接", () => {
    // 手工向单例池注入一条「逻辑属于 task 2 但 turn 键形如 temp-x」的连接，
    // 验证 removeByTask(1) 不会断开它——确认断开依据的是登记的 taskId 而非键名前缀。
    let flushed = false;
    const fake = {
      connection: { disconnect: () => { (fake as unknown as { _d: boolean })._d = true; } },
      taskId: 2,
      flush: () => {
        flushed = true;
      },
    } as never;
    sseConnectionPool.replace("temp-x", fake);
    const removed = sseConnectionPool.removeByTask(1);
    expect(removed).toEqual([]);
    expect((fake as unknown as { _d: boolean })._d).toBeFalsy();
    // 归属不同 task 的条目不得被冲刷：flush 必须按条目精确回调，而非批量冲刷。
    expect(flushed).toBe(false);
  });

  // 单测连接池 removeByTask：同一 task 下多个 turn（含 temp- 键）全部断开。
  it("removeByTask 断开同一 task 下全部 turn（含不同键形态）", () => {
    let flushed = false;
    const fakeTemp = {
      connection: { disconnect: () => { (fakeTemp as unknown as { _d: boolean })._d = true; } },
      taskId: 5,
      flush: () => {
        flushed = true;
      },
    } as never;
    sseConnectionPool.replace("temp-y", fakeTemp);
    const removed = sseConnectionPool.removeByTask(5);
    expect(removed).toContain("temp-y");
    expect((fakeTemp as unknown as { _d: boolean })._d).toBe(true);
    // 断开前必须冲刷该连接自己的缓冲，否则 disconnect 前已到达的事件会滞留丢失。
    expect(flushed).toBe(true);
  });

  // 阻断级回归：条目已从池中移除后若 flush 抛异常，disconnect 仍必须执行。
  // 否则该连接成为「已出池但仍活着」的泄漏连接：池操作再也够不着它，它却继续
  // 投递事件，在各 store 中重建已删任务的条目。
  it("flush 抛异常时连接仍被断开，不泄漏", () => {
    let disconnected = false;
    const fake = {
      connection: {
        disconnect: () => {
          disconnected = true;
        },
      },
      taskId: 11,
      flush: () => {
        throw new Error("缓冲冲刷失败");
      },
    } as never;
    sseConnectionPool.replace("temp-leak", fake);

    expect(() => sseConnectionPool.remove("temp-leak")).not.toThrow();
    expect(disconnected).toBe(true);
  });

  // 批量断开时单条 flush 抛异常，不得中断其余条目的断开。
  it("批量断开时单条 flush 抛异常不影响其余条目", () => {
    const disconnectedIds: number[] = [];
    const makeEntry = (taskId: number, shouldThrow: boolean) =>
      ({
        connection: { disconnect: () => disconnectedIds.push(taskId) },
        taskId,
        flush: shouldThrow
          ? () => {
              throw new Error("缓冲冲刷失败");
            }
          : () => {},
      }) as never;

    sseConnectionPool.replace("a-1", makeEntry(21, true));
    sseConnectionPool.replace("a-2", makeEntry(21, false));
    sseConnectionPool.replace("a-3", makeEntry(22, false));

    const removed = sseConnectionPool.removeByTask(21);

    expect(removed.sort()).toEqual(["a-1", "a-2"]);
    // 抛异常那条与后续那条都必须已断开。
    expect(disconnectedIds.sort((a, b) => a - b)).toEqual([21, 21]);
    // 组外 task 不受影响。
    expect(sseConnectionPool.get("a-3")).toBeDefined();
  });
});

describe("同一 flush 批次内同一 task 的多个 turn 交错", () => {
  // 同一 task 的两个 turn（101、102）在同一帧交错投递，每个 turn 各 2 条，
  // 最终该 task 应取「本批次内整体最后一条」(turn 102 的 9000)。
  it("同一 task 多 turn 同批次交错取本批次最后一条", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
      await connectHandle()(1, 102);
    });
    const conn101 = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const conn102 = FakeSSEConnection.instances.find((c) => c.turnId === "102")!;

    act(() => {
      conn101.onEvent(usageEvent(1, 101, 1000, 200000));
      conn102.onEvent(usageEvent(1, 102, 3000, 200000));
      conn101.onEvent(usageEvent(1, 101, 5000, 200000));
      conn102.onEvent(usageEvent(1, 102, 9000, 200000));
    });
    flushFrame();

    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(9000);
  });

  // 同批次含多 task 多 turn：task 1 取 turn 102 的 9000，task 2 取 turn 202 的 7000。
  it("同批次多 task 多 turn 各自独立取最后一条", async () => {
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
      await connectHandle()(1, 102);
      await connectHandle()(2, 201);
      await connectHandle()(2, 202);
    });
    const conn101 = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const conn102 = FakeSSEConnection.instances.find((c) => c.turnId === "102")!;
    const conn201 = FakeSSEConnection.instances.find((c) => c.turnId === "201")!;
    const conn202 = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;

    act(() => {
      conn101.onEvent(usageEvent(1, 101, 1000, 200000));
      conn201.onEvent(usageEvent(2, 201, 4000, 128000));
      conn102.onEvent(usageEvent(1, 102, 9000, 200000));
      conn202.onEvent(usageEvent(2, 202, 7000, 128000));
    });
    flushFrame();

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]?.usedTokens).toBe(9000);
    expect(state.usageByTaskId[2]?.usedTokens).toBe(7000);
  });
});

describe("deleteTask 后端失败时的缓存保持（AC11）", () => {
  // 后端 reject 时，本地 usage 缓存与 taskStore 必须保持不变（任务未被删除）。
  it("deleteTask 后端失败时本地缓存与 task 记录保持不变", async () => {
    // 预置 task 1 的真实记录与占用。
    act(() => {
      useTaskStore.setState({
        tasksById: { 1: { task_id: 1, workspace_id: 1 } as never },
      });
    });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "t");
    apiMocks.deleteTask.mockRejectedValue(new Error("backend 500"));

    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });

    await act(async () => {
      await expect(deleteTaskHandle()(1)).rejects.toThrow("backend 500");
    });

    // 缓存未清。
    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(1000);
    // task 记录未删。
    expect(useTaskStore.getState().tasksById[1]).toBeDefined();
    // 连接已断开（断流先于后端调用，所以即便后端失败也已断流）。
    const conn = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    expect(conn.disconnected).toBe(true);
  });

  // 断流后再调后端失败：残留流已断开，不得重建被删 task 的占用。
  it("deleteTask 断流后后端失败，残留流不再重建已删 task 条目", async () => {
    apiMocks.deleteTask.mockRejectedValue(new Error("backend 500"));
    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });
    useContextUsageStore.getState().setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "t");

    await act(async () => {
      await expect(deleteTaskHandle()(1)).rejects.toThrow("backend 500");
    });

    const stale = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    act(() => {
      stale.onEvent(usageEvent(1, 101, 999999, 200000));
    });
    flushFrame();

    // 流已断，事件被丢弃，task 1 无条目（注意：2 的条目仍在，因 delete 失败只影响 task 1）。
    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeUndefined();
    expect(useContextUsageStore.getState().usageByTaskId[2]?.usedTokens).toBe(5000);
  });
});

describe("快速连续 deleteTask 同一 task（并发）", () => {
  // 并发两次 deleteTask：第二次应为 no-op（task 已被第一次删除），但都不应抛错、
  // 且 usage 缓存最终为空、后端 deleteTask 被调用两次（幂等由后端保证，前端不拦截）。
  it("并发两次 deleteTask 不抛错，最终缓存清空", async () => {
    act(() => {
      useTaskStore.setState({
        tasksById: { 1: { task_id: 1, workspace_id: 1 } as never },
      });
    });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "t");
    apiMocks.deleteTask.mockResolvedValue(undefined);

    render(<Harness />);
    await act(async () => {
      await connectHandle()(1, 101);
    });

    let rejected = false;
    await act(async () => {
      const p1 = deleteTaskHandle()(1);
      const p2 = deleteTaskHandle()(1);
      try {
        await Promise.all([p1, p2]);
      } catch {
        rejected = true;
      }
    });

    expect(rejected).toBe(false);
    expect(useContextUsageStore.getState().usageByTaskId[1]).toBeUndefined();
    expect(useTaskStore.getState().tasksById[1]).toBeUndefined();
  });
});
