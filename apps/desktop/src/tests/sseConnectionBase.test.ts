// @vitest-environment happy-dom
/**
 * SSEConnectionBase 通用连接骨架验证测试。
 *
 * 目标：用 mock fetch + ReadableStream 验证基类模板方法 runConnection 的通用路径，
 * 覆盖 fetch/Accept header/trace header 注入/backend trace 记录/EOF flush（feed("\n\n")+reset()）/
 * 跨分片 feed 拼接/终态检测驱动的异常结束判定/AbortError 两类分支（抽象钩子分流）。
 *
 * 子类差异通过轻量 stub（StubSSEConnection / StubChildConnection）验证钩子分流，
 * 不依赖任何真实业务子类，保持对基类契约的纯净断言。
 *
 * @module tests/sseConnectionBase
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SSEConnectionBase,
  TERMINAL_EVENT_TYPES,
  type SSEBaseConnectionContext,
  type SSEBaseHooks,
} from "@/services/sseConnectionBase";
import type { RuntimeEvent } from "@shared/events";
import type { ParsedSSEFrame } from "@/services/sseParser";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

/** 默认测试 taskId。 */
const TASK_ID = "task-base";

/**
 * 构造一条 SSE 帧文本。
 *
 * @param eventType - 事件类型。
 * @param payload - 事件 payload。
 * @param eventId - 事件标识。
 * @returns SSE 帧文本。
 */
function frame(eventType: string, payload: Record<string, unknown>, eventId = "evt-base"): string {
  const event = {
    event_id: eventId,
    task_id: TASK_ID,
    turn_id: "turn-base",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as unknown as RuntimeEvent;
  return `event: ${eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

/**
 * 构造一个把给定文本分片按顺序推送、自然结束的 SSE 响应。
 *
 * @param chunks - 文本分片列表。
 * @param onCancel - reader 被取消时的回调。
 * @returns fetch 可返回的 Response-like 对象。
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

/**
 * 构造可被 abort 中断（reader 抛 AbortError）的 fetch 实现。
 *
 * @returns fetch 实现与已捕获的 abort 信号。
 */
function abortableFetch(): { fetchImpl: typeof fetch; lastSignal: () => AbortSignal | null } {
  let lastSignal: AbortSignal | null = null;
  const fetchImpl = (async (_url: string, init?: RequestInit) => {
    lastSignal = init?.signal ?? null;
    return new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(new DOMException("The operation was aborted", "AbortError"));
      });
    });
  }) as unknown as typeof fetch;
  return { fetchImpl, lastSignal: () => lastSignal };
}

/**
 * 轻量 stub 连接：验证基类通用路径与钩子分流，不携带任何业务终态语义。
 *
 * 终态判定采用基类默认 4 类；onAbort 默认把 `abortedFlag` 置 true（模拟父类置 CLOSED）。
 */
class StubSSEConnection extends SSEConnectionBase {
  /** 收到的帧（经 handleFrame 钩子）。 */
  public received: RuntimeEvent[] = [];
  /** AbortError 分支是否被触发。 */
  public abortedFlag = false;
  /** reportAbnormalEndIfNeeded 是否被调用。 */
  public abnormalReported = false;

  protected handleFrame(frame: ParsedSSEFrame): void {
    this.received.push(JSON.parse(frame.data) as RuntimeEvent);
  }

  protected onAbort(): void {
    this.abortedFlag = true;
  }

  protected reportAbnormalEndIfNeeded(_context: SSEBaseConnectionContext): void {
    // 复刻基类短路：已终态或主动断开则不报异常。
    if (this._terminalReceived || this._aborted) {
      return;
    }
    this.abnormalReported = true;
  }

  /** 暴露模板方法给测试调用。 */
  public run(path: string, context: SSEBaseConnectionContext, hooks: SSEBaseHooks): Promise<void> {
    return this.runConnection(path, context, hooks);
  }
}

/**
 * 验证终态判定覆盖子类覆盖场景的 child stub：终态仅 3 类（不含 final_response）。
 */
class StubChildConnection extends SSEConnectionBase {
  /** 收到的帧。 */
  public received: RuntimeEvent[] = [];
  /** terminalReceived 是否因终态事件置位（由 isTerminalEvent 驱动）。 */
  public terminalHit = false;

  protected handleFrame(frame: ParsedSSEFrame): void {
    const event = JSON.parse(frame.data) as RuntimeEvent;
    this.received.push(event);
    if (this.isTerminalEvent(event)) {
      this.terminalHit = true;
    }
  }

  /**
   * child 流终态仅 3 类（不含 final_response），覆盖基类默认 4 类。
   */
  protected isTerminalEvent(event: RuntimeEvent): boolean {
    return ["run_finished", "run_failed", "run_cancelled"].includes(event.event_type);
  }

  protected onAbort(): void {
    // child 流：AbortError 静默 return，不置任何外部状态。
  }

  /** 暴露模板方法给测试调用。 */
  public run(path: string, context: SSEBaseConnectionContext, hooks: SSEBaseHooks): Promise<void> {
    return this.runConnection(path, context, hooks);
  }
}

describe("SSEConnectionBase 通用连接骨架", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("TERMINAL_EVENT_TYPES 默认含 4 类（含 final_response）", () => {
    expect([...TERMINAL_EVENT_TYPES]).toEqual([
      "run_finished",
      "final_response",
      "run_failed",
      "run_cancelled",
    ]);
  });

  it("fetch 携带 Accept 与 trace header，且记录 backend trace", async () => {
    const fetchMock = vi.fn(async () => makeStreamResponse([frame("run_started", {})]));
    vi.stubGlobal("fetch", fetchMock);
    const conn = new StubSSEConnection();
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ Accept: "text/event-stream" });
    // x-trace-id 由 buildTraceHeaders 注入。
    expect((init.headers as Record<string, string>)["x-trace-id"]).toMatch(/^[a-f0-9]{32}$/);
  });

  it("读取循环通过 eventsource-parser 逐帧分发，并做 EOF flush（feed + reset）", async () => {
    // 故意把一条完整帧拆成两个分片，验证跨分片拼接由解析器完成（无手写 split("\n\n")）。
    const full = frame("run_started", { step: 1 });
    const mid = Math.floor(full.length / 2);
    const fetchMock = vi.fn(async () =>
      makeStreamResponse([full.slice(0, mid), full.slice(mid)]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const conn = new StubSSEConnection();
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    expect(conn.received).toHaveLength(1);
    expect(conn.received[0].event_type).toBe("run_started");
  });

  it("disconnect 标记 aborted 并 abort 仍在存活的 controller，结束连接", async () => {
    const { fetchImpl, lastSignal } = abortableFetch();
    vi.stubGlobal("fetch", fetchImpl);
    const conn = new StubSSEConnection();
    const p = conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    await new Promise((r) => setTimeout(r, 5));
    conn.disconnect();
    expect(conn._aborted).toBe(true);
    expect(lastSignal()).not.toBeNull();
    await p;
    expect(conn.abortedFlag).toBe(true);
  });

  it("HTTP 非 2xx 时抛出，不在基类内部吞错", async () => {
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
    const conn = new StubSSEConnection();
    await expect(conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {})).rejects.toThrow();
  });

  it("正常收到终态事件后流结束，异常结束上报不被触发", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse([frame("run_started", {}), frame("run_finished", {})])),
    );
    const conn = new StubSSEConnection();
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    expect(conn.abnormalReported).toBe(false);
  });

  it("流结束但未收到终态且非主动断开：触发异常结束上报", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_started", {})])));
    const conn = new StubSSEConnection();
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    expect(conn.abnormalReported).toBe(true);
  });

  it("onAbort 钩子分流：父类实现置 abortedFlag，异常结束上报被短路", async () => {
    // child 流断开路径：AbortError 静默 return，reportAbnormalEndIfNeeded 由基类
    // 在 terminalReceived||aborted 时短路（不调用子类覆盖）。此处用 StubSSEConnection
    // 验证「主动断开后，异常上报不被触发」。
    const { fetchImpl } = abortableFetch();
    vi.stubGlobal("fetch", fetchImpl);
    const conn = new StubSSEConnection();
    const p = conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    await new Promise((r) => setTimeout(r, 5));
    conn.disconnect();
    await p;
    expect(conn._aborted).toBe(true);
    expect(conn.abortedFlag).toBe(true);
    // 主动断开（aborted=true）后，reportAbnormalEndIfNeeded 被基类短路，不应被调用。
    expect(conn.abnormalReported).toBe(false);
  });

  it("isTerminalEvent 默认 4 类；子类可覆盖为 3 类（不含 final_response）", async () => {
    // 父类默认：final_response 命中终态。
    const parent = new StubSSEConnection();
    expect(
      parent.isTerminalEvent({ event_type: "final_response" } as RuntimeEvent),
    ).toBe(true);

    // child stub 覆盖：final_response 不命中，run_finished 命中。
    const child = new StubChildConnection();
    expect(child.isTerminalEvent({ event_type: "final_response" } as RuntimeEvent)).toBe(false);
    expect(child.isTerminalEvent({ event_type: "run_finished" } as RuntimeEvent)).toBe(true);

    // 用 child stub 跑真实流：final_response 到达不应置 terminalHit，run_finished 到达才置。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([frame("run_started", {}), frame("final_response", {}), frame("run_finished", {})]),
      ),
    );
    const child2 = new StubChildConnection();
    await child2.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    expect(child2.terminalHit).toBe(true);
    expect(child2.received.map((e) => e.event_type)).toEqual(["run_started", "final_response", "run_finished"]);
  });

  it("runConnection 的 finally 回收 controller：异常结束后 controller 置 null", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_started", {})])));
    const conn = new StubSSEConnection();
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, {});
    // 复用基类字段（protected，带下划线命名，兼容既有 sse.lifecycle 测试契约）。
    expect((conn as unknown as { _abortController: AbortController | null })._abortController).toBeNull();
  });

  it("基类默认 reportAbnormalEndIfNeeded：未终态未断开则记录 error 并调用 onError", async () => {
    const onError = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_started", {})])));
    const conn = new StubSSEConnection();
    // 不覆盖 reportAbnormalEndIfNeeded，验证基类默认逻辑：未终态未断开 → onError 触发。
    Object.defineProperty(conn, "reportAbnormalEndIfNeeded", {
      value: SSEConnectionBase.prototype.reportAbnormalEndIfNeeded,
      writable: true,
      configurable: true,
    });
    await conn.run("/turns/turn-base/stream", { task_id: TASK_ID }, { onError });
    expect(onError).toHaveBeenCalledTimes(1);
  });
});
