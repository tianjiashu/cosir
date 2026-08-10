// @vitest-environment happy-dom
/**
 * 异常断流 → turn 写 failed 的链路验证测试。
 *
 * 本次思考块折叠修复的**前提假设**是：后端崩溃/断流、终态事件一条都没到达时，
 * 前端能自行把 turn 判定为终态。若该前提不成立（turn 永远停在 running），
 * 则 `selectVisibleEntries(state, isTurnActive)` 的 `isTurnActive` 永远为 true，
 * 折叠修复完全失效 —— 而所有纯函数测试与组件测试仍会全绿。
 * 本文件用真实 `SSEConnection` + mock fetch 端到端锁死这条前提链路：
 *
 *   流结束但 _terminalReceived=false 且非主动 abort
 *     → SSEConnection._reportStreamEndIfAbnormal
 *     → onError → useSSE.markFailed
 *     → turnStore.updateTurn(status:"failed") / taskStore / setStreamingTurn(null)
 *
 * @module tests/useSSE.abnormalStreamEnd
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { TurnRecord } from "@shared/turn";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

import { useSSE } from "@/hooks/useSSE";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";

/** 构造一条 SSE 帧文本。 */
function frame(eventType: string, payload: Record<string, unknown>, eventId: string): string {
  // 用 Record<string, unknown> 而非 Partial<RuntimeEvent>：
  // RuntimeEvent 是 discriminated union，TS 对 Partial<union> 的判别成员赋值会报类型不兼容
  // （无法匹配单一成员）。SSE 帧只是序列化文本，用宽类型即可，避免无谓的类型摩擦。
  const event: Record<string, unknown> = {
    event_id: eventId,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    sequence: 1,
    created_at: new Date().toISOString(),
  };
  return `event: ${eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** 构造一个把给定文本分片推送后自然结束的 SSE 响应。 */
function makeStreamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i]));
      i += 1;
    },
  });
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers(),
    body: stream,
  } as unknown as Response;
}

/** 预置一个 running 状态的 turn，模拟流式进行中。 */
function seedRunningTurn(): void {
  const turn = {
    turn_id: "turn-1",
    task_id: "task-1",
    input_text: "帮我分析一下",
    status: "running",
    end_reason: null,
    response_text: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as unknown as TurnRecord;
  useTurnStore.getState().setTurnsForTask("task-1", [turn]);
  useTurnStore.getState().setStreamingTurn("turn-1");
}

/** 读取当前 turn-1 的记录。 */
function readTurn(): TurnRecord | undefined {
  return useTurnStore.getState().turnsByTaskId["task-1"]?.find((t) => t.turn_id === "turn-1");
}

describe("异常断流 → markFailed 链路（思考块折叠的前提条件）", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useEventStore.getState().clearEvents();
    useTurnStore.setState({ turnsByTaskId: {}, streamingTurnId: null });
    seedRunningTurn();
  });

  it("思考中断流、无任何终态事件：turn 必须被写成 failed", async () => {
    // 测试目的：锁死折叠修复所依赖的前提 —— 后端崩溃时前端能自愈判定终态。
    // 可能发现的缺陷：若 _reportStreamEndIfAbnormal 的判定条件退化（如误把
    //   model_thinking_delta 当终态、或 onError 未接到 markFailed），turn 会永远停在
    //   running，思考块折叠修复将完全失效（原 bug 无声复活）。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", {}, "e0"),
          frame("model_thinking_delta", { text: "我先分析一下" }, "e1"),
          // 流到此突然结束：run_failed / run_finished / final_response 一条都没有
        ]),
      ),
    );

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
      // 等待流读完 + finally 兜底 flush + rAF
      await new Promise((r) => setTimeout(r, 80));
    });

    const turn = readTurn();
    expect(turn?.status).toBe("failed");
    expect(turn?.end_reason).toBe("SSE stream ended without terminal event");
    // 流式标记必须清除，否则上层仍会当作活动 turn 处理。
    expect(useTurnStore.getState().streamingTurnId).toBeNull();
  });

  it("thinking 事件已入 store：断流后事件不丢，仅 turn 状态转终态", async () => {
    // 测试目的：验证折叠场景下「内容仍在、只是状态变了」，
    //   这正是 TurnTimeline 能渲染出折叠思考块（而非空白）的数据基础。
    // 可能发现的缺陷：断流时连同缓冲事件一并丢弃，则 UI 上思考块会整个消失。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", {}, "e0"),
          frame("model_thinking_delta", { text: "被中断的思考" }, "e1"),
        ]),
      ),
    );

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
      await new Promise((r) => setTimeout(r, 80));
    });

    // 事件本身不丢（这部分行为正确）
    const shard = useEventStore.getState().eventsByTurnId["turn-1"] ?? [];
    expect(shard.map((e) => e.event_id)).toContain("e1");
    // turn 必须处于终态，否则思考块不会折叠
    expect(readTurn()?.status).toBe("failed");
  });

  it("时序竞态护栏：残留 run_started flush 不得把 markFailed 的终态覆盖回 running", async () => {
    // 测试目的：锁死「异常断流 → markFailed」链路在时序竞态下的正确性。
    // 原缺陷：SSEConnection 在流读完时**同步**触发 onError → markFailed（turn 先写 failed），
    //   但 run_started 仍滞留在 useSSE 的 rAF 攒批缓冲中，随后 connect().finally() 的
    //   兜底 flush 才 syncRuntimeStatus(run_started) 把它映射回 running，覆盖终态。
    //   结果：异常断流后 turn 永久停在 running → isTurnActive 恒为 true → 思考块永不折叠。
    // 修复：onError 先记录 pendingError，待残留事件 flush 完再应用 markFailed，
    //   保证终态写序在最后，不被迟到 flush 覆盖。
    // 本用例还原真实写序，断言「failed 必须是最终落地态」。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", {}, "e0"),
          frame("model_thinking_delta", { text: "思考" }, "e1"),
        ]),
      ),
    );

    // 按时间顺序记录每一次 turnStore 写入，还原真实写序。
    const writes: Array<{ status?: string; streaming: string | null }> = [];
    const unsubscribe = useTurnStore.subscribe((s) => {
      writes.push({
        status: s.turnsByTaskId["task-1"]?.[0]?.status,
        streaming: s.streamingTurnId,
      });
    });

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
      await new Promise((r) => setTimeout(r, 80));
    });
    unsubscribe();

    const statusSequence = writes.map((w) => w.status);
    // 证据 1：markFailed 确实执行过，failed 一度被正确写入。
    expect(statusSequence).toContain("failed");
    // 证据 2（修复护栏）：最终落地态必须是 failed —— 不得被迟到的 run_started 覆盖回 running。
    expect(statusSequence[statusSequence.length - 1]).toBe("failed");
    // 证据 3：failed 之后不得再出现 running（终态一旦写入即不再回退）。
    const lastFailedIdx = statusSequence.lastIndexOf("failed");
    const lastRunningIdx = statusSequence.lastIndexOf("running");
    expect(lastRunningIdx).toBeLessThan(lastFailedIdx);
  });

  it("正常收到 run_finished：turn 转 completed 而非 failed（反向护栏）", async () => {
    // 测试目的：防止「一律 markFailed」式的过度实现。
    // 可能发现的缺陷：若异常判定失去 _terminalReceived 短路，正常完成的 turn
    //   也会被误标 failed，用户每轮对话都出现失败提示。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", {}, "e0"),
          frame("model_thinking_delta", { text: "思考" }, "e1"),
          frame("run_finished", {}, "e2"),
        ]),
      ),
    );

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
      await new Promise((r) => setTimeout(r, 80));
    });

    expect(readTurn()?.status).toBe("completed");
  });

  it("run_cancelled 终态：turn 转 cancelled，同样进入非活动态", async () => {
    // 测试目的：cancelled 也是折叠触发条件之一（isTurnActive=false），需确认链路能产出该状态。
    // 可能发现的缺陷：cancelled 被误映射为 running/completed，导致取消后思考块仍展开。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", {}, "e0"),
          frame("model_thinking_delta", { text: "思考" }, "e1"),
          frame("run_cancelled", {}, "e2"),
        ]),
      ),
    );

    const { result } = renderHook(() => useSSE());
    await act(async () => {
      await result.current.connect("task-1", "turn-1");
      await new Promise((r) => setTimeout(r, 80));
    });

    const status = readTurn()?.status;
    expect(status).toBe("cancelled");
    // 断言其确实落在「非活动态」集合内 —— 这是折叠的判定依据。
    expect(["pending", "running"]).not.toContain(status);
  });
});
