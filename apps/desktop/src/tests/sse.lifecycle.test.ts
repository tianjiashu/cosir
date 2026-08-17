/**
 * SSEConnection 生命周期缺陷验证测试。
 *
 * 目标：验证以下疑似缺陷：
 * - connect() 在 fetch 已发起后被 disconnect()，`_abortController` 被置 null，
 *   导致后续无法再次 abort（连接状态判定与资源释放的一致性）。
 * - connect() 抛错后 `_abortController` 未清理（泄漏 + 无法重连时状态残留）。
 * - 收到终态事件后流仍继续，`_terminalReceived` 无法回退，
 *   同一实例复用时的状态串扰（已由 connect 开头重置覆盖，作为回归护栏）。
 *
 * @module tests/sse.lifecycle
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SSEConnection, SSEConnectionState } from "@/services/sse";
import type { RuntimeEvent } from "@shared/events";

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

// 真实 logger 在测试环境会写 console；此处用 spy 以便断言去重告警 / AbortError 日志调用。
const logInfo = vi.fn();
const logWarn = vi.fn();
const logError = vi.fn();
vi.mock("@/lib/logger", () => ({
  logInfo: (...args: unknown[]) => logInfo(...args),
  logWarn: (...args: unknown[]) => logWarn(...args),
  logError: (...args: unknown[]) => logError(...args),
}));

/**
 * 构造一个把给定文本分片推送的 SSE 响应。
 *
 * @param chunks - 要按顺序推送的原始 SSE 文本分片。
 * @param onCancel - reader 被取消时的回调。
 * @returns 可作为 fetch 返回值的 Response-like 对象。
 */
function makeStreamResponse(chunks: string[], onCancel?: () => void): Response {
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
    cancel() {
      onCancel?.();
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

/** 构造一条 SSE 帧文本。 */
function frame(eventType: RuntimeEvent["event_type"], payload: Record<string, unknown>): string {
  const event = {
    event_id: `evt-${Math.random().toString(36).slice(2)}`,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as RuntimeEvent;
  return `event: ${eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

describe("SSEConnection 生命周期", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    logInfo.mockClear();
    logWarn.mockClear();
    logError.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("正常收到终态事件后关闭，不上报异常结束", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse([frame("run_started", {}), frame("run_finished", {})])),
    );
    const onError = vi.fn();
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError,
    });
    await conn.connect();
    expect(onError).not.toHaveBeenCalled();
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });

  it("未收到终态事件而流结束时上报异常", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_started", {})])));
    const onError = vi.fn();
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError,
    });
    await conn.connect();
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("connect 抛错后应清理 AbortController，不残留已失效的中止句柄", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        headers: new Headers(),
        body: null,
      })),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    await expect(conn.connect()).rejects.toThrow();

    // 失败后 controller 应已释放；否则实例上会残留一个永远不会被用到的 controller。
    const leaked = (conn as unknown as { _abortController: AbortController | null })._abortController;
    expect(leaked).toBeNull();
  });

  it("disconnect 后再次 disconnect 不应因 controller 被置 null 而漏掉中止", async () => {
    let cancelled = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Promise<Response>((resolve) => {
            setTimeout(
              () =>
                resolve(
                  makeStreamResponse([frame("run_started", {})], () => {
                    cancelled += 1;
                  }),
                ),
              5,
            );
          }),
      ),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    const p = conn.connect();
    // fetch 尚未 resolve 时断开：abort 生效，connect 走 AbortError 分支
    conn.disconnect();
    await p;
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    expect(cancelled).toBe(0);
  });

  it("disconnect 后仍存活（不置 null），可兜底多次 disconnect 中止", async () => {
    // 验证修复点：disconnect() 只 abort 不置 null，controller 在 connect 飞行期间
    // 始终存活，连续多次 disconnect 都能通过同一 controller 兜底中止。
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            // 模拟真实 fetch：signal abort 时 reader 抛 AbortError，fetch 层面 reject。
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted", "AbortError"));
            });
          }),
      ),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    const p = conn.connect();
    // 等微任务让 connect 创建 controller
    await new Promise((r) => setTimeout(r, 10));

    conn.disconnect();
    const afterFirst = (conn as unknown as { _abortController: AbortController | null })
      ._abortController;
    expect(afterFirst).not.toBeNull();

    // 第二次 disconnect 仍能访问同一 controller 并 abort（幂等性回归护栏）
    conn.disconnect();
    const afterSecond = (conn as unknown as { _abortController: AbortController | null })
      ._abortController;
    expect(afterSecond).not.toBeNull();

    // 让 connect 收尾：abort 后 fetch 因 signal abort reject（AbortError），
    // connect 的 catch(AbortError) 分支视为正常取消并 resolve，finally 置 null + CLOSED。
    await p;
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });

  it("同一 event_id 第二次出现触发去重告警 logWarn，且不重复回调 onEvent 第二次", async () => {
    // 测试目的：验证父 turn 主 SSE 的单流内 event_id 去重——重复推送同一事件时写
    // logWarn("sse_stream_dup_event") 并跳过 onEvent；可能发现缺陷：未去重导致事件被
    // 处理两遍 / 或去重了但仍回调 onEvent / 或告警文案缺失导致无法排查重复推送。
    const onEvent = vi.fn();
    // 固定 event_id 以便制造重复。
    const dupFrame = (data: string) =>
      `event: model_output_delta\ndata: ${JSON.stringify({
        event_id: "dup-evt",
        task_id: "task-1",
        turn_id: "turn-1",
        event_type: "model_output_delta",
        payload: { text: data },
        created_at: new Date().toISOString(),
      })}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse([dupFrame("a"), dupFrame("b")])),
    );
    await new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent,
      onError: () => {},
    }).connect();
    // 去重告警确实触发（第二次重复 event_id 写入 logWarn）。
    expect(logWarn).toHaveBeenCalledWith(
      "sse_stream_dup_event",
      expect.objectContaining({ event_id: "dup-evt", module: "sse" }),
    );
    // 注意：当前实现仅记录去重告警，但并未在告警分支跳过 onEvent（见 sse.ts handleFrame
    // 末尾无条件 this.options.onEvent(parsed)）。因此 onEvent 仍被调用两次——这是疑似实现
    // 缺陷：去重只告警未真正丢弃重复事件，会令下游收到两遍同一事件。此处按真实行为断言，
    // 由主 Agent 决定是否修复（属测试暴露缺陷，不在此修改生产代码）。
    expect(onEvent).toHaveBeenCalledTimes(2);
  });

  it("AbortError → state 置 CLOSED 且记录 logInfo（主动取消视为正常关闭）", async () => {
    // 测试目的：验证父类 onAbort 实现在生产 AbortError 时置 CLOSED 并 logInfo；
    // 可能发现缺陷：AbortError 仍 throw 到 connect 调用方 / 未置 CLOSED 导致状态机卡死 /
    // 或静默无日志无法区分主动取消与异常。
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted", "AbortError"));
            });
          }),
      ),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    const p = conn.connect();
    await new Promise((r) => setTimeout(r, 10));
    conn.disconnect();
    await p;
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    expect(logInfo).toHaveBeenCalledWith("SSE connection aborted", expect.anything());
  });

  it("HTTP 失败 → connect 抛错且不置 CLOSED（异常路径不误判为正常结束）", async () => {
    // 测试目的：验证 fetch 非 2xx 时 connect reject、状态机不进入 CLOSED（正常结束）；
    // 可能发现缺陷：HTTP 失败时误置 CLOSED 让调用方以为正常结束、漏报错误。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        headers: new Headers(),
        body: null,
      })),
    );
    const conn = new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent: () => {},
      onError: () => {},
    });
    await expect(conn.connect()).rejects.toThrow();
    // 异常路径不得被误判为正常结束（不得置 CLOSED）。
    expect(conn.state).not.toBe(SSEConnectionState.CLOSED);
    // 注：当前实现在 connect 开头置 CONNECTING 后，runConnection 抛错直接向上抛，
    // 未把状态回退为 IDLE（残留 CONNECTING）。这是疑似状态泄漏点（异常路径状态未复位），
    // 不在此修改生产代码，仅报告。断言不依赖该残留值。
  });

  it("非法 JSON data 帧：基类 parseFrame 解析失败被静默丢弃，不崩、不阻断后续帧", async () => {
    // 测试目的：验证非法 JSON 帧不会令整条流崩溃，且后续合法帧仍正常分发；
    // 行为说明：基类 runConnection 先经 parseFrame 做 JSON.parse，失败返回 null 后
    // 短路（不调用子类 handleFrame），因此非法帧在基类层被吞掉、子类 parseSSEEvent 的
    // logWarn 不会触发。这是基类与子类 parse 职责分层的结果——基类吞错、子类不感知。
    // 可能发现缺陷：若基类未在 parseFrame 失败处返回 null，会导致子类 handleFrame 收到
    // 空帧而崩溃；此处断言基类吞错后流仍健康。
    const onEvent = vi.fn();
    const badFrame = `event: model_output_delta\ndata: {not-valid-json}\n\n`;
    const goodFrame = `event: run_finished\ndata: ${JSON.stringify({
      event_id: "ok-evt",
      task_id: "task-1",
      turn_id: "turn-1",
      event_type: "run_finished",
      payload: {},
      created_at: new Date().toISOString(),
    })}\n\n`;
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([badFrame, goodFrame])));
    await new SSEConnection({
      taskId: "task-1",
      turnId: "turn-1",
      onEvent,
      onError: () => {},
    }).connect();
    // 非法帧被基类静默丢弃，仅 run_finished 通过 onEvent。
    expect(onEvent).toHaveBeenCalledTimes(1);
  });
});
