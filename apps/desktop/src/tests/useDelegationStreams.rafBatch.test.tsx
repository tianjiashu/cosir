// @vitest-environment happy-dom
/**
 * 修复 1「child SSE 流对齐父流 rAF 攒批」的独立验证测试。
 *
 * 被测行为契约（apps/desktop/src/hooks/useDelegationStreams.ts）：
 * child 流的 onEvent 不再逐条 appendEvents([event])，而是推入 pendingEventsRef 缓冲，
 * 由 requestAnimationFrame 在下一动画帧一次性 flush（单次 appendEvents(batch)）。
 * flush 在 effect 清理（taskId 变更 / 卸载）与 effect 重跑（连接重建）时兜底调用。
 *
 * 本文件只验证「攒批不改变可观测语义」：不丢事件、顺序正确、多流互不串台、切换兜底，
 * 并额外验证「确实攒批了」（appendEvents 调用次数 << 事件条数）。
 *
 * 测试手段说明：
 * - 不 mock DelegationStreamConnection 的构造函数（该类是 hook 直接 new 的具体实现），
 *   而是通过 spyOn 其 prototype.connect 捕获 this.options.onEvent，从而以「真实 hook 传入的
 *   回调」精确模拟 SSE 逐条推事件。这比 mock 整个类更贴近真实链路，也不需要改业务代码。
 * - happy-dom 提供 requestAnimationFrame，但其调度依赖真实宏任务；为让「未 flush 状态」
 *   可确定性观测，本文件对 rAF 做可控 stub（手动 runFrame），避免时序竞态导致 flaky。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { RuntimeEvent } from "@shared/events";
import { useDelegationStreams } from "@/hooks/useDelegationStreams";
import { useEventStore } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";
import { DelegationStreamConnection } from "@/services/delegationStream";
import * as api from "@/services/api";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api");
  return {
    ...actual,
    listTaskEvents: vi.fn(),
  };
});

const TASK_ID = "task-raf-batch";
const TASK_ID_2 = "task-raf-batch-2";
const PARENT_TURN_ID = "turn-parent";

/**
 * 构造测试用 RuntimeEvent。
 *
 * @param eventId - 事件标识。
 * @param eventType - 运行时事件类型。
 * @param turnId - 归属 turn。
 * @param payload - 事件 payload。
 * @param sequence - 排序序号。
 * @param taskId - 归属 task。
 * @returns 测试事件。
 */
function runtimeEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  turnId: string,
  payload: Record<string, unknown>,
  sequence: number,
  taskId: string = TASK_ID,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: taskId,
    turn_id: turnId,
    sequence,
    created_at: new Date(Date.UTC(2026, 7, 11, 0, 0, 0) + sequence * 1000).toISOString(),
    payload,
  } as RuntimeEvent;
}

/**
 * 构造 delegation_child_started 事件（让 deriveDelegationStreams 识别出 child turn）。
 *
 * @param delegationId - 委派标识。
 * @param childTurnId - child turn 标识。
 * @param sequence - 排序序号。
 * @param taskId - 归属 task。
 * @returns 委派子流启动事件。
 */
function delegationChildStarted(
  delegationId: string,
  childTurnId: string,
  sequence: number,
  taskId: string = TASK_ID,
): RuntimeEvent {
  return runtimeEvent(
    `child-started-${childTurnId}`,
    "delegation_child_started",
    PARENT_TURN_ID,
    {
      delegation_id: delegationId,
      parent_turn_id: PARENT_TURN_ID,
      child_turn_id: childTurnId,
      child_agent_id: "delegate_reviewer",
      delegation_type: "review",
      status: "running",
    },
    sequence,
    taskId,
  );
}

/**
 * 可控 rAF：把回调排入队列，由测试显式 runFrame 触发，模拟「一帧」。
 */
interface FrameHarness {
  /** 执行当前排队的所有帧回调（模拟浏览器进入下一动画帧）。 */
  runFrame: () => void;
  /** 当前尚未执行的帧回调数量。 */
  pendingFrames: () => number;
}

/**
 * 安装可控的 requestAnimationFrame / cancelAnimationFrame。
 *
 * @returns 帧控制句柄。
 * @sideeffect 通过 vi.stubGlobal 覆盖全局 rAF/cAF，afterEach 由 unstubAllGlobals 还原。
 */
function installControllableRaf(): FrameHarness {
  let nextHandle = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback): number => {
    const handle = nextHandle++;
    callbacks.set(handle, cb);
    return handle;
  });
  vi.stubGlobal("cancelAnimationFrame", (handle: number): void => {
    callbacks.delete(handle);
  });
  return {
    runFrame: () => {
      const snapshot = [...callbacks.entries()];
      callbacks.clear();
      for (const [, cb] of snapshot) {
        cb(performance.now());
      }
    },
    pendingFrames: () => callbacks.size,
  };
}

/**
 * 捕获 hook 为每个 child turn 传入的 onEvent 回调。
 *
 * 通过 spy DelegationStreamConnection.prototype.connect（并使其永不 resolve，
 * 模拟长连接保持打开）读取 this.options，从而拿到真实回调。
 *
 * @returns childTurnId -> onEvent 的映射与还原函数。
 */
function captureChildEventHandlers(): {
  handlers: Map<string, (event: RuntimeEvent) => void>;
  restore: () => void;
} {
  const handlers = new Map<string, (event: RuntimeEvent) => void>();
  const connectSpy = vi
    .spyOn(DelegationStreamConnection.prototype, "connect")
    .mockImplementation(function (this: DelegationStreamConnection) {
      const options = (this as unknown as {
        options: { childTurnId: string; onEvent: (event: RuntimeEvent) => void };
      }).options;
      handlers.set(options.childTurnId, options.onEvent);
      // 永不 resolve：模拟 SSE 长连接保持打开，避免 connect().finally 删除连接映射。
      return new Promise<void>(() => {});
    });
  const disconnectSpy = vi
    .spyOn(DelegationStreamConnection.prototype, "disconnect")
    .mockImplementation(() => {});
  return {
    handlers,
    restore: () => {
      connectSpy.mockRestore();
      disconnectSpy.mockRestore();
    },
  };
}

/** appendEvents 的函数签名（用于替换 store action 时保持类型正确）。 */
type AppendEventsFn = (incoming: RuntimeEvent[]) => void;

/**
 * 用 spy 包裹 eventStore.appendEvents 并写回 store。
 *
 * store 的 action 引用在 hook 内被 useEventStore(state => state.appendEvents) 读取，
 * 直接 spyOn(getState()) 不会替换 store 内引用，必须 setState 写回。
 *
 * @returns 包含 spy 的句柄。
 * @sideeffect 修改 eventStore 的 appendEvents 引用（beforeEach 会整体重置状态）。
 */
function spyOnAppendEvents(): { spy: ReturnType<typeof vi.fn> } {
  const original = useEventStore.getState().appendEvents;
  const spy = vi.fn((incoming: RuntimeEvent[]) => {
    original(incoming);
  });
  useEventStore.setState({ appendEvents: spy as AppendEventsFn });
  return { spy };
}

/**
 * 读取指定 turn 分片的 event_id 序列。
 *
 * @param turnId - turn 标识。
 * @returns 该分片按 store 顺序的 event_id 列表。
 */
function shardEventIds(turnId: string): string[] {
  return (useEventStore.getState().eventsByTurnId[turnId] ?? []).map((event) => event.event_id);
}

describe("useDelegationStreams | child 流 rAF 攒批（修复 1）", () => {
  let frames: FrameHarness;
  let captured: ReturnType<typeof captureChildEventHandlers>;
  // 真实 appendEvents 引用：部分用例会用 spy 替换 store action，需在每个用例前还原，
  // 避免 spy 跨用例泄漏影响其他断言。
  const realAppendEvents = useEventStore.getState().appendEvents;

  beforeEach(() => {
    vi.clearAllMocks();
    useEventStore.setState({ appendEvents: realAppendEvents });
    vi.mocked(api.listTaskEvents).mockResolvedValue([]);
    useEventStore.setState({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      connectionState: SSEConnectionState.IDLE,
      processedEventIds: new Set<string>(),
    });
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("fetch should not be called: connect is mocked");
    }));
    frames = installControllableRaf();
    captured = captureChildEventHandlers();
  });

  afterEach(() => {
    captured.restore();
    vi.unstubAllGlobals();
  });

  // 测试目的：验证「事件不丢」——对单个 child turn 连续 push 20 条事件，一帧 flush 后
  // eventsByTurnId 分片必须包含全部 20 条，且数量精确等于 20。
  // 可能发现的缺陷：缓冲被覆盖而非追加（丢事件）、flush 只提交最后一条、
  // flush 后未清空导致重复、或 rAF 未排程导致事件永久滞留在缓冲。
  it("事件不丢：单 child 连续 push 20 条，一帧 flush 后分片完整且数量精确", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });

    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    const pushed = Array.from({ length: 20 }, (_, index) =>
      runtimeEvent(`c1-delta-${index}`, "model_output_delta", "turn-child-1", { text: `t${index}` }, 10 + index),
    );

    await act(async () => {
      for (const event of pushed) {
        onEvent(event);
      }
    });

    // 攒批语义前置断言：尚未进入下一帧时，事件仍在缓冲、未写入 store。
    expect(shardEventIds("turn-child-1")).toHaveLength(0);
    expect(frames.pendingFrames()).toBe(1);

    await act(async () => {
      frames.runFrame();
    });

    expect(shardEventIds("turn-child-1")).toHaveLength(20);
    expect(shardEventIds("turn-child-1")).toEqual(pushed.map((event) => event.event_id));
  });

  // 测试目的：验证「攒批确实生效」——同一帧内 30 条事件只应触发 1 次 appendEvents 调用，
  // 而非 30 次；这是本次修复的性能契约本体。
  // 可能发现的缺陷：改动未生效仍逐条 appendEvents；或 scheduleFlush 非幂等导致多次 flush
  // 把一批拆成多次 set（丧失攒批收益）。
  it("攒批生效：同一帧 30 条事件只提交一次 appendEvents", async () => {
    const { spy: appendSpy } = spyOnAppendEvents();

    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    appendSpy.mockClear();
    await act(async () => {
      for (let index = 0; index < 30; index += 1) {
        onEvent(
          runtimeEvent(`batch-${index}`, "model_output_delta", "turn-child-1", { text: `x${index}` }, 100 + index),
        );
      }
    });
    expect(appendSpy).toHaveBeenCalledTimes(0);

    await act(async () => {
      frames.runFrame();
    });

    expect(appendSpy).toHaveBeenCalledTimes(1);
    expect(appendSpy.mock.calls[0][0]).toHaveLength(30);
    expect(shardEventIds("turn-child-1")).toHaveLength(30);
  });

  // 测试目的：验证「顺序正确」——按 sequence 递增顺序 push，flush 后 store 分片顺序
  // 必须与 push 顺序严格一致（逐项精确比较，非集合包含）。
  // 可能发现的缺陷：缓冲用 unshift / 反向遍历导致顺序反转；批量提交时顺序被打乱。
  it("顺序正确：递增 sequence 的 15 条事件 flush 后顺序与 push 顺序严格一致", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    const ordered = Array.from({ length: 15 }, (_, index) =>
      runtimeEvent(`ord-${String(index).padStart(2, "0")}`, "model_output_delta", "turn-child-1", { i: index }, 200 + index),
    );

    await act(async () => {
      for (const event of ordered) {
        onEvent(event);
      }
      frames.runFrame();
    });

    expect(shardEventIds("turn-child-1")).toEqual(ordered.map((event) => event.event_id));
    // 同时校验 sequence 单调递增，排除「id 顺序对但事件对象错位」。
    const sequences = (useEventStore.getState().eventsByTurnId["turn-child-1"] ?? []).map((e) => e.sequence);
    expect(sequences).toEqual(ordered.map((event) => event.sequence));
  });

  // 测试目的：验证「跨帧多批也不丢不乱」——分三帧各 push 5 条，累计 15 条，
  // 每帧 flush 后累计数量与顺序都必须正确。
  // 可能发现的缺陷：flush 后 rafRef 未置空导致后续帧不再排程（第二批起丢事件）；
  // 或缓冲未清空导致前批重复提交（被 event_id 去重掩盖但数量异常）。
  it("跨帧多批：三帧各 5 条，累计 15 条不丢不重且顺序正确", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    const all: RuntimeEvent[] = [];
    for (let frame = 0; frame < 3; frame += 1) {
      const batch = Array.from({ length: 5 }, (_, index) =>
        runtimeEvent(
          `f${frame}-e${index}`,
          "model_output_delta",
          "turn-child-1",
          { frame, index },
          300 + frame * 5 + index,
        ),
      );
      all.push(...batch);
      await act(async () => {
        for (const event of batch) {
          onEvent(event);
        }
        frames.runFrame();
      });
      expect(shardEventIds("turn-child-1")).toHaveLength((frame + 1) * 5);
    }

    expect(shardEventIds("turn-child-1")).toEqual(all.map((event) => event.event_id));
  });

  // 测试目的：验证「多流合并」——3 个并发 child turn 交错高频 push（每个 10 条），
  // 一帧 flush 后三个分片各自完整（各 10 条）、顺序正确、互不串台（无跨 turn 混入）。
  // 可能发现的缺陷：共享缓冲导致事件被写进错误 turn 分片；某个 child 的事件被其他
  // child 的 flush 吞掉；scheduleFlush 幂等判断导致后注册 child 的事件漏排程。
  it("多流合并：3 个并发 child 交错 push，各分片完整且互不串台", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    const childIds = ["turn-child-a", "turn-child-b", "turn-child-c"];
    await act(async () => {
      childIds.forEach((childTurnId, index) => {
        useEventStore.getState().appendEvent(delegationChildStarted(`d-${childTurnId}`, childTurnId, index + 1));
      });
    });

    await waitFor(() => {
      expect(childIds.every((id) => captured.handlers.has(id))).toBe(true);
    });

    const expectedByChild = new Map<string, string[]>(childIds.map((id) => [id, []]));
    await act(async () => {
      // 交错推送：a0,b0,c0,a1,b1,c1,... 模拟三流同时流式。
      for (let round = 0; round < 10; round += 1) {
        childIds.forEach((childTurnId, childIndex) => {
          const eventId = `${childTurnId}-r${round}`;
          expectedByChild.get(childTurnId)!.push(eventId);
          captured.handlers.get(childTurnId)!(
            runtimeEvent(
              eventId,
              "model_output_delta",
              childTurnId,
              { round },
              400 + round * 3 + childIndex,
            ),
          );
        });
      }
      frames.runFrame();
    });

    for (const childTurnId of childIds) {
      const actual = shardEventIds(childTurnId);
      // 各分片数量精确、顺序一致。
      expect(actual).toEqual(expectedByChild.get(childTurnId));
      // 互不串台：分片内不得出现其他 child 的事件。
      expect(actual.every((id) => id.startsWith(childTurnId))).toBe(true);
      expect(
        (useEventStore.getState().eventsByTurnId[childTurnId] ?? []).every((e) => e.turn_id === childTurnId),
      ).toBe(true);
    }
    // 扁平 events 总量 = 3 个父 delegation 事件 + 30 条 child 事件。
    expect(useEventStore.getState().eventsByTaskId[TASK_ID] ?? []).toHaveLength(33);
  });

  // 测试目的：验证「任务切换兜底 flush」——已 push 但未到帧时切换 taskId，
  // effect 清理路径的 flush 必须把残留事件提交进原 child 分片，不得丢失。
  // 可能发现的缺陷：清理函数未调用 flush（残留缓冲随连接断开被丢弃）；
  // 或 flush 时机在 connections.clear() 之后仍读到已重置的缓冲导致丢事件。
  it("任务切换兜底：未 flush 的残留事件在 taskId 变更时被兜底提交，不丢失", async () => {
    const { rerender } = renderHook(({ taskId }: { taskId: string }) => useDelegationStreams(taskId), {
      initialProps: { taskId: TASK_ID },
    });
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    const residual = Array.from({ length: 7 }, (_, index) =>
      runtimeEvent(`residual-${index}`, "model_output_delta", "turn-child-1", { index }, 500 + index),
    );
    await act(async () => {
      for (const event of residual) {
        onEvent(event);
      }
    });
    // 前置：尚未 flush，store 中不应有这些事件。
    expect(shardEventIds("turn-child-1")).toHaveLength(0);

    // 切换 taskId（不执行 runFrame，验证兜底 flush 而非帧 flush）。
    await act(async () => {
      rerender({ taskId: TASK_ID_2 });
    });

    expect(shardEventIds("turn-child-1")).toHaveLength(7);
    expect(shardEventIds("turn-child-1")).toEqual(residual.map((event) => event.event_id));
  });

  // 测试目的：验证「卸载兜底 flush」——已 push 未到帧时 unmount，残留事件仍须落库。
  // 可能发现的缺陷：仅在 taskId 变更时 flush、卸载路径遗漏，导致最后一批事件丢失。
  it("卸载兜底：未 flush 的残留事件在 hook unmount 时被兜底提交", async () => {
    const { unmount } = renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    await act(async () => {
      onEvent(runtimeEvent("unmount-1", "model_output_delta", "turn-child-1", {}, 600));
      onEvent(runtimeEvent("unmount-2", "model_output_delta", "turn-child-1", {}, 601));
    });
    expect(shardEventIds("turn-child-1")).toHaveLength(0);

    await act(async () => {
      unmount();
    });

    expect(shardEventIds("turn-child-1")).toEqual(["unmount-1", "unmount-2"]);
  });

  // 测试目的：验证「effect 重跑兜底 flush」——父 task 新增事件触发 effect 重跑时，
  // 残留缓冲应在重跑内被 flush（hook 中 flush() 位于连接回收前）。
  // 可能发现的缺陷：effect 重跑路径缺少 flush，导致连接被重建/回收时缓冲事件丢失。
  it("effect 重跑兜底：父事件到达触发重跑时残留缓冲被提交", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    await act(async () => {
      onEvent(runtimeEvent("rerun-1", "model_output_delta", "turn-child-1", {}, 700));
    });
    expect(shardEventIds("turn-child-1")).toHaveLength(0);

    // 父 task 新事件 → taskEvents 变更 → effect 重跑 → 内部 flush 兜底。
    await act(async () => {
      useEventStore
        .getState()
        .appendEvent(runtimeEvent("parent-note", "model_output_delta", PARENT_TURN_ID, {}, 701));
    });

    expect(shardEventIds("turn-child-1")).toEqual(["rerun-1"]);
  });

  // 测试目的：验证「批内乱序被 store 归位」——攒批把乱序到达的事件合并成一批提交，
  // 结果分片仍须按 sequence 有序（依赖 eventStore.appendOrderedShard 的口径）。
  // 可能发现的缺陷：攒批后整批直接 concat 而未逐条归位，导致乱序传导到渲染层。
  it("批内乱序：同帧乱序到达的事件 flush 后按 sequence 归位", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    await act(async () => {
      onEvent(runtimeEvent("seq-803", "model_output_delta", "turn-child-1", {}, 803));
      onEvent(runtimeEvent("seq-801", "model_output_delta", "turn-child-1", {}, 801));
      onEvent(runtimeEvent("seq-802", "model_output_delta", "turn-child-1", {}, 802));
      frames.runFrame();
    });

    expect(shardEventIds("turn-child-1")).toEqual(["seq-801", "seq-802", "seq-803"]);
  });

  // 测试目的：验证「重复 event_id 幂等」——攒批批内含重复事件时，去重后分片仍只有一条，
  // 攒批不得放大重复写入。
  // 可能发现的缺陷：攒批绕过 event_id 去重导致同一事件被追加两次（内容翻倍类缺陷）。
  it("幂等去重：同帧内重复 event_id 只落库一条", async () => {
    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    const dup = runtimeEvent("dup-1", "model_output_delta", "turn-child-1", { text: "same" }, 900);
    await act(async () => {
      onEvent(dup);
      onEvent(dup);
      onEvent(runtimeEvent("dup-2", "model_output_delta", "turn-child-1", {}, 901));
      frames.runFrame();
    });

    expect(shardEventIds("turn-child-1")).toEqual(["dup-1", "dup-2"]);
  });

  // 测试目的：验证「空缓冲 flush 无副作用」——无待提交事件时的兜底 flush 不应调用
  // appendEvents（避免无意义 set 击穿下游 memo）。
  // 可能发现的缺陷：flush 未做空批短路，每次 effect 重跑都触发一次空 set。
  it("空批短路：无残留事件时的兜底 flush 不调用 appendEvents", async () => {
    const { spy: appendSpy } = spyOnAppendEvents();

    const { rerender } = renderHook(({ taskId }: { taskId: string }) => useDelegationStreams(taskId), {
      initialProps: { taskId: TASK_ID },
    });
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });

    appendSpy.mockClear();
    await act(async () => {
      frames.runFrame();
      rerender({ taskId: TASK_ID_2 });
    });

    expect(appendSpy).not.toHaveBeenCalled();
  });

  // 测试目的：验证「rAF 不可用时退化为 setTimeout」——移除全局 rAF 后事件仍能在
  // ~16ms 后被 flush，不丢失（覆盖 scheduleFlush 的降级分支）。
  // 可能发现的缺陷：降级分支未实现或句柄类型混用导致 cancel 失败/事件永久滞留。
  it("降级分支：环境无 requestAnimationFrame 时经 setTimeout 仍能 flush", async () => {
    vi.stubGlobal("requestAnimationFrame", undefined);
    vi.stubGlobal("cancelAnimationFrame", undefined);

    renderHook(() => useDelegationStreams(TASK_ID));
    await act(async () => {
      useEventStore.getState().appendEvent(delegationChildStarted("d-1", "turn-child-1", 1));
    });
    await waitFor(() => {
      expect(captured.handlers.has("turn-child-1")).toBe(true);
    });
    const onEvent = captured.handlers.get("turn-child-1")!;

    await act(async () => {
      onEvent(runtimeEvent("fallback-1", "model_output_delta", "turn-child-1", {}, 1000));
      onEvent(runtimeEvent("fallback-2", "model_output_delta", "turn-child-1", {}, 1001));
    });

    await waitFor(
      () => {
        expect(shardEventIds("turn-child-1")).toEqual(["fallback-1", "fallback-2"]);
      },
      { timeout: 1000 },
    );
  });
});
