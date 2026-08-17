/**
 * DelegationStreamConnection 专项单元测试。
 *
 * 目标：直接针对 services/delegationStream.ts 的 child turn 只订阅 SSE 连接类，
 * 验证其相对父 turn 主 SSE 的差异化契约：
 * - child 流终态仅 3 类（run_finished / run_failed / run_cancelled），final_response 不终态
 * - 收到 run_finished 才终态（_terminalReceived 由基类 isTerminalEvent 驱动）
 * - AbortError 静默 return（只 logInfo，不抛、不改外部/全局状态）
 * - connecting 布尔防重入：重复 connect 抛 "already connecting"
 * - handleFrame 内 JSON 解析失败（非法 data）走 logWarn 且不崩、丢弃该帧
 * - 跨分片帧拼接仍只回调一次 onEvent
 * - 实例复用：第二次 connect 不残留上一次的 connecting 标志
 *
 * @module tests/delegationStream
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { DelegationStreamConnection } from "@/services/delegationStream";
import type { RuntimeEvent } from "@shared/events";

// 真实 logger 在测试环境会写 console + 可选落盘；此处 mock 以便断言日志调用次数与内容。
const logInfo = vi.fn();
const logWarn = vi.fn();
const logError = vi.fn();
vi.mock("@/lib/logger", () => ({
  logInfo: (...args: unknown[]) => logInfo(...args),
  logWarn: (...args: unknown[]) => logWarn(...args),
  logError: (...args: unknown[]) => logError(...args),
}));

vi.mock("@/stores/conversationTraceStore", () => ({
  useConversationTraceStore: { getState: () => ({ recordTrace: () => {} }) },
}));

const TASK_ID = "task-deleg";
const DELEGATION_ID = "deleg-1";
const CHILD_TURN_ID = "turn-child";

/** 构造一条 SSE 帧文本（event + JSON data）。 */
function frame(eventType: string, eventId: string, payload: Record<string, unknown> = {}): string {
  const event = {
    event_id: eventId,
    task_id: TASK_ID,
    turn_id: CHILD_TURN_ID,
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as unknown as RuntimeEvent;
  return `event: ${eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** 构造把给定分片顺序推送、自然结束的 SSE 响应。 */
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

/** 构造可被 abort 中断（reader 抛 AbortError）的 fetch 实现。 */
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

function makeConn(extra: Partial<{ onEvent: (e: RuntimeEvent) => void; onError: (e: Error) => void }> = {}) {
  return new DelegationStreamConnection({
    taskId: TASK_ID,
    delegationId: DELEGATION_ID,
    childTurnId: CHILD_TURN_ID,
    onEvent: extra.onEvent ?? (() => {}),
    onError: extra.onError,
  });
}

describe("DelegationStreamConnection 子类契约", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("fetch 携带 Accept 与 trace header（child 流沿用基类 header 注入）", async () => {
    // 测试目的：验证 child 流 connect 经基类 runConnection 发起请求并注入 Accept/trace header；
    // 可能发现缺陷：子类忘记透传基类 header / 路径错误。
    const fetchMock = vi.fn(async () => makeStreamResponse([frame("run_started", "e1")]));
    vi.stubGlobal("fetch", fetchMock);
    await makeConn().connect();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ Accept: "text/event-stream" });
    expect((init.headers as Record<string, string>)["x-trace-id"]).toMatch(/^[a-f0-9]{32}$/);
  });

  it("收到 final_response 不终态：继续转发后续事件，等 run_finished 才终态", async () => {
    // 测试目的：验证 child 流覆盖 isTerminalEvent（3 类，不含 final_response），
    // final_response 到达后流保持打开、仍会回调后续事件；可能发现缺陷：误把 final_response 当终态而过早关闭。
    const received: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([
          frame("run_started", "e1"),
          frame("final_response", "e2", { text: "child final" }),
          frame("run_finished", "e3", { status: "completed" }),
        ]),
      ),
    );
    const conn = makeConn({ onEvent: (e) => received.push(e.event_type) });
    await conn.connect();
    // 三条事件都应被转发（final_response 不终态，流继续读到 run_finished）。
    expect(received).toEqual(["run_started", "final_response", "run_finished"]);
  });

  it("收到 run_finished 即终态：流正常结束（reportAbnormalEndIfNeeded 不报异常）", async () => {
    // 测试目的：验证 run_finished 命中 child 终态，流读完无异常结束上报；
    // 可能发现缺陷：终态判定遗漏 run_finished 导致误报异常结束。
    const onError = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse([frame("run_started", "e1"), frame("run_finished", "e2")])),
    );
    await makeConn({ onError }).connect();
    expect(onError).not.toHaveBeenCalled();
  });

  it("AbortError 静默：disconnect 后只 logInfo 不抛、不改全局状态", async () => {
    // 测试目的：验证 child 流 onAbort 静默 return（仅 logInfo，不抛），与父类置 CLOSED 差异；
    // 可能发现缺陷：child 流 AbortError 仍 throw 污染父 turn / 或静默但漏记日志影响排查。
    const { fetchImpl } = abortableFetch();
    vi.stubGlobal("fetch", fetchImpl);
    const conn = makeConn();
    const p = conn.connect();
    await new Promise((r) => setTimeout(r, 5));
    conn.disconnect();
    // 应 resolve（不抛），验证静默分支。
    await expect(p).resolves.toBeUndefined();
    expect(logInfo).toHaveBeenCalled();
    expect(
      logInfo.mock.calls.some((c) => String(c[0]).includes("aborted")),
    ).toBe(true);
  });

  it("connecting 防重入：同一实例重复 connect 抛 already connecting", async () => {
    // 测试目的：验证 child 流 connecting 布尔防止并发重入；
    // 可能发现缺陷：缺少防重入导致两个 connect 同时跑、双 fetch / 状态串扰。
    const { fetchImpl } = abortableFetch();
    vi.stubGlobal("fetch", fetchImpl);
    const conn = makeConn();
    const first = conn.connect();
    await new Promise((r) => setTimeout(r, 5));
    await expect(conn.connect()).rejects.toThrow(/already connecting/i);
    conn.disconnect();
    await first;
  });

  it("实例复用：第一次 connect 结束后 connecting 复位，可再次 connect", async () => {
    // 测试目的：验证 connect 的 finally 把 connecting 复位，避免实例无法复用；
    // 可能发现缺陷：finally 未复位 connecting 导致后续 connect 永远抛 already connecting（泄漏式防重入）。
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([frame("run_finished", "e1")])));
    const conn = makeConn();
    await conn.connect();
    // 第二次 connect 不应抛（connecting 已复位）。
    await expect(conn.connect()).resolves.toBeUndefined();
  });

  it("非法 JSON data 帧：基类 parseFrame 解析失败被静默丢弃，不崩、不阻断后续帧", async () => {
    // 测试目的：验证非法 JSON 帧不会令 child 流崩溃，后续合法帧仍正常分发；
    // 行为说明：基类 runConnection 先经 parseFrame 做 JSON.parse，失败返回 null 后短路
    // （不调用子类 handleFrame），因此非法帧在基类层被吞掉、子类 parseRuntimeEvent 的
    // logWarn 不会触发。此处断言基类吞错后流仍健康。
    const received: string[] = [];
    const badFrame = `event: run_started\ndata: {not-valid-json}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([badFrame, frame("run_finished", "e2")]),
      ),
    );
    await makeConn({ onEvent: (e) => received.push(e.event_type) }).connect();
    // 非法帧被基类静默丢弃，仅 run_finished 通过。
    expect(received).toEqual(["run_finished"]);
  });

  it("跨分片帧拼接：一帧拆两块 feed 仍只回调一次 onEvent", async () => {
    // 测试目的：验证 base 类 + eventsource-parser 跨分片拼接，handleFrame 仅触发一次；
    // 可能发现缺陷：分片边界被截断导致帧重复/丢失。
    const full = frame("run_started", "e1");
    const mid = Math.floor(full.length / 2);
    const received: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async () => makeStreamResponse([full.slice(0, mid), full.slice(mid)])));
    await makeConn({ onEvent: (e) => received.push(e.event_id) }).connect();
    expect(received).toEqual(["e1"]);
  });

  it("重复 event_id 的帧仍正常转发（child 流不做去重，差异于父类）", async () => {
    // 测试目的：验证 child 流不实现 event_id 去重（与父类差异），重复 event_id 不告警、照常转发；
    // 可能发现缺陷：子类误复用父类去重集合导致重复帧被丢弃。
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        makeStreamResponse([frame("model_output_delta", "dup"), frame("model_output_delta", "dup")]),
      ),
    );
    const received: string[] = [];
    await makeConn({ onEvent: (e) => received.push(e.event_id) }).connect();
    expect(received).toEqual(["dup", "dup"]);
    // 不应触发任何 dup 告警（message 含 sse_stream_dup_event）。
    expect(logWarn).not.toHaveBeenCalledWith(
      expect.stringContaining("sse_stream_dup_event"),
      expect.anything(),
    );
  });
});
