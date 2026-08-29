// @vitest-environment happy-dom
/**
 * 并发 task 的 CONTEXT_USAGE 事件隔离（经真实 useSSE 攒批 flush 路径）。
 *
 * 守护不变量：后台 task 的占用事件只写入它自己的 task 条目，不得覆盖用户当前正在
 * 查看的 task 的圆环。
 *
 * 真实使用 useSSE（不 mock 该 hook），仅 mock 底层 ``@/services/sse`` 以手动驱动
 * 事件到达；contextUsageStore / taskStore / turnStore / eventStore 全部为真实实例。
 *
 * 可证伪性：旧实现在 flush 循环中执行 ``setContextUsage(payload, createdAt)``
 * （全局覆盖、不读 ``event.task_id``），本文件核心用例在旧实现下必然失败——task A
 * 的占用会被 task B 的事件覆盖。
 *
 * @module tests/useSSE.contextUsageIsolation
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";

// ---- 手动驱动的 rAF：useSSE 用 requestAnimationFrame 攒批，测试需显式 flush ----
const rafCallbacks: FrameRequestCallback[] = [];

vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
  rafCallbacks.push(cb);
  return rafCallbacks.length;
});
vi.stubGlobal("cancelAnimationFrame", () => {});

// ---- 假 SSE 连接：捕获实例以便手动投递事件 ----
// vi.mock 会被提升到文件顶部，工厂内引用的任何顶层绑定都必须经 vi.hoisted 声明，
// 否则工厂求值时该绑定尚未初始化（ReferenceError）。
const { FakeSSEConnection } = vi.hoisted(() => {
  class FakeSSEConnection {
    static instances: FakeSSEConnection[] = [];
    readonly taskId: string;
    readonly turnId: string;
    // RuntimeEvent 为类型-only 引用（编译期擦除），在 vi.hoisted 工厂中标注安全。
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
      this.onEvent = options.onEvent;
      FakeSSEConnection.instances.push(this);
    }

    /**
     * 返回永不 resolve 的 Promise，模拟「流保持打开」。
     *
     * 不能返回已 resolve 的 Promise：真实 useSSE 在 ``connect().finally()`` 中判定
     * 连接已自然结束后会把它从连接池移除。立即 resolve 会让连接在 ``disconnectTask``
     * 被调用前就自行出池，测不到按 task 维度批量断开的行为。
     */
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

import { useSSE } from "@/hooks/useSSE";
import { useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useEventStore } from "@/stores/eventStore";

/** 持有 useSSE 返回动作的最小测试组件。 */
function Harness() {
  const sse = useSSE();
  (window as unknown as { __sse?: typeof sse }).__sse = sse;
  return null;
}

/** 取出 Harness 暴露的 useSSE 返回值。 */
function sseHandle() {
  return (window as unknown as { __sse: ReturnType<typeof useSSE> }).__sse;
}

/** 驱动一次攒批 flush（执行所有已排入 rAF 的回调）。 */
function flushFrame() {
  act(() => {
    const pending = rafCallbacks.splice(0, rafCallbacks.length);
    for (const cb of pending) {
      cb(0);
    }
  });
}

/** 构造一条 context_usage 运行时事件。 */
function usageEvent(taskId: number, turnId: number, used: number, total: number): RuntimeEvent {
  return {
    event_id: `evt-${taskId}-${turnId}-${used}`,
    event_type: "context_usage",
    task_id: taskId,
    turn_id: turnId,
    sequence: 1,
    created_at: "2026-08-28T10:00:00Z",
    payload: { used_tokens: used, total_tokens: total },
  } as RuntimeEvent;
}

beforeEach(() => {
  cleanup();
  rafCallbacks.length = 0;
  FakeSSEConnection.instances = [];
  useContextUsageStore.setState({ usageByTaskId: {} });
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    drafts: {},
  });
  useTurnStore.setState({
    turnsByTaskId: {},
    streamingTurnIds: {},
  } as never);
  // 本用例不校验事件历史，只需清空其按 task 分片的缓存以免跨用例串味。
  useEventStore.setState({ eventsByTaskId: {} } as never);
});

describe("并发 task 的 context_usage 事件隔离", () => {
  // 核心回归（对应缺陷 D1）：后台 task B 的占用事件到达后，active task A 的占用不变。
  it("后台 task B 的占用事件不覆盖 active task A 的占用", async () => {
    render(<Harness />);
    const { connect } = sseHandle();
    await act(async () => {
      await connect(1, 101);
      await connect(2, 202);
    });

    const connA = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const connB = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;

    // A 先到达并 flush。
    act(() => {
      connA.onEvent(usageEvent(1, 101, 1000, 200000));
    });
    flushFrame();
    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(1000);

    // 后台 B 到达并 flush：A 的条目必须原样保留。
    act(() => {
      connB.onEvent(usageEvent(2, 202, 90000, 128000));
    });
    flushFrame();

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]?.usedTokens).toBe(1000);
    expect(state.usageByTaskId[1]?.totalTokens).toBe(200000);
    expect(state.usageByTaskId[2]?.usedTokens).toBe(90000);
  });

  // 建连不得清零任何 task 的已缓存占用（对应缺陷 D2）。
  it("新建连接不清零已有 task 的占用缓存", async () => {
    useContextUsageStore.getState().setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");

    render(<Harness />);
    const { connect } = sseHandle();
    await act(async () => {
      await connect(2, 202);
    });

    // 旧实现在 connect() 中执行全局 reset()，此处会归零。
    expect(useContextUsageStore.getState().usageByTaskId[1]?.usedTokens).toBe(1000);
  });

  // 同一 flush 批次内交错多个 task 的事件，每个 task 各自取本批次最后一条（AC8）。
  it("同一批次内交错的多 task 事件各自取本批次最后一条", async () => {
    render(<Harness />);
    const { connect } = sseHandle();
    await act(async () => {
      await connect(1, 101);
      await connect(2, 202);
    });

    const connA = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const connB = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;

    act(() => {
      connA.onEvent(usageEvent(1, 101, 1000, 200000));
      connB.onEvent(usageEvent(2, 202, 5000, 128000));
      connA.onEvent(usageEvent(1, 101, 2000, 200000));
      connB.onEvent(usageEvent(2, 202, 7000, 128000));
    });
    flushFrame();

    const state = useContextUsageStore.getState();
    expect(state.usageByTaskId[1]?.usedTokens).toBe(2000);
    expect(state.usageByTaskId[2]?.usedTokens).toBe(7000);
  });

  // disconnectTask 断开该 task 下全部连接，不影响其它 task（对应「删除运行中任务」残留）。
  it("disconnectTask 只断开指定 task 的连接，不影响其它 task", async () => {
    render(<Harness />);
    const { connect, disconnectTask } = sseHandle();
    await act(async () => {
      await connect(1, 101);
      await connect(1, 102);
      await connect(2, 202);
    });

    act(() => {
      disconnectTask(1);
    });

    const connA1 = FakeSSEConnection.instances.find((c) => c.turnId === "101")!;
    const connA2 = FakeSSEConnection.instances.find((c) => c.turnId === "102")!;
    const connB = FakeSSEConnection.instances.find((c) => c.turnId === "202")!;
    expect(connA1.disconnected).toBe(true);
    expect(connA2.disconnected).toBe(true);
    expect(connB.disconnected).toBe(false);
  });
});
