import { describe, it, expect, vi, afterEach } from "vitest";
import { SSEConnection, SSEConnectionState } from "@/services/sse";
import type { RuntimeEvent } from "@shared/events";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useClientTraceStore.getState().reset();
  useConversationTraceStore.getState().resetConversationTraces();
});

function makeStreamResponse(body: string, status = 200): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return {
    ok: status < 400,
    status,
    statusText: status < 400 ? "OK" : "Error",
    headers: new Headers({
      "x-trace-id": "abcdef1234567890abcdef1234567890",
    }),
    body: stream,
  } as unknown as Response;
}

describe("sse.ts — SSEConnectionState 枚举", () => {
  it("枚举值正确（非字符串字面量误用）", () => {
    expect(SSEConnectionState.IDLE).toBe("idle");
    expect(SSEConnectionState.CONNECTING).toBe("connecting");
    expect(SSEConnectionState.STREAMING).toBe("streaming");
    expect(SSEConnectionState.CLOSED).toBe("closed");
  });

  it("初始状态为 IDLE", () => {
    const conn = new SSEConnection({ taskId: "t1", onEvent: () => {} });
    expect(conn.state).toBe(SSEConnectionState.IDLE);
  });

  it("connect 成功流程：状态 IDLE→CONNECTING→STREAMING→CLOSED，并解析事件", async () => {
    const event: RuntimeEvent = {
      event_id: "e1",
      event_type: "step_started",
      task_id: "t1",
      created_at: new Date().toISOString(),
      payload: { step_type: "x", step_index: 0 },
    };
    const body = `event: step_started\ndata: ${JSON.stringify(event)}\n\n`;
    const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => makeStreamResponse(body));
    vi.stubGlobal(
      "fetch",
      fetchImpl,
    );
    const onEvent = vi.fn();
    const onStateChange = vi.fn();
    const onTrace = vi.fn();
    const conn = new SSEConnection({
      taskId: "t1",
      onEvent,
      onStateChange,
      onTrace,
    });
    await conn.connect();
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith(event);
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    const init = fetchImpl.mock.calls[0][1] as unknown as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Accept).toBe("text/event-stream");
    expect(headers["x-trace-id"]).toMatch(/^[0-9a-f]{32}$/);
    expect(onTrace).toHaveBeenCalledWith(headers["x-trace-id"]);
    expect(useClientTraceStore.getState().lastTrace?.traceId).toBe("abcdef1234567890abcdef1234567890");
    expect(useConversationTraceStore.getState().latestTraceByTaskId.t1).toMatchObject({
      traceId: headers["x-trace-id"],
      taskId: "t1",
      operation: "task_stream",
      method: "GET",
      path: "/tasks/t1/stream",
    });
    // 状态变更顺序包含 CONNECTING、STREAMING、CLOSED
    const states = onStateChange.mock.calls.map((c) => c[0]);
    expect(states).toContain(SSEConnectionState.CONNECTING);
    expect(states).toContain(SSEConnectionState.STREAMING);
    expect(states).toContain(SSEConnectionState.CLOSED);
  });

  it("重复 connect（已 CONNECTING）抛出错误", async () => {
    // fetch 返回一个 ok 响应，但 body reader 在 abort 时 reject（模拟真实流可被取消）
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url, init) => {
        const signal = (init as { signal?: AbortSignal })?.signal;
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            signal?.addEventListener("abort", () => {
              controller.error(new Error("aborted"));
            });
          },
        });
        return {
          ok: true,
          status: 200,
          statusText: "OK",
          body: stream,
        } as unknown as Response;
      }),
    );
    const conn = new SSEConnection({ taskId: "t1", onEvent: () => {} });
    const p = conn.connect(); // 进入 CONNECTING → STREAMING，卡在 reader.read()
    // 给事件循环一个 tick，确保 connect 的同步部分（_setState(CONNECTING)）已执行
    await new Promise((r) => setTimeout(r, 10));
    expect(conn.state).toBe(SSEConnectionState.STREAMING);
    // 立即再次 connect，应同步抛错
    await expect(conn.connect()).rejects.toThrow("SSE 连接已在进行中");
    conn.disconnect();
    // 第一次 connect 应因 AbortError 正常结束
    await p.catch(() => {});
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });

  it("SSE 断流（fetch 抛错）→ onError 回调且状态 CLOSED", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const onError = vi.fn();
    const onStateChange = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("stream broken");
      }),
    );
    const conn = new SSEConnection({
      taskId: "t1",
      onEvent: () => {},
      onError,
      onStateChange,
    });
    await expect(conn.connect()).rejects.toThrow("stream broken");
    expect(onError).toHaveBeenCalledTimes(1);
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    const ctx = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(ctx.trace_id).toMatch(/^[0-9a-f]{32}$/);
  });

  it("SSE 非 2xx 响应 → 抛出错误且状态 CLOSED", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => makeStreamResponse("", 500)),
    );
    const onError = vi.fn();
    const conn = new SSEConnection({
      taskId: "t1",
      onEvent: () => {},
      onError,
    });
    await expect(conn.connect()).rejects.toThrow(/HTTP 500/);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
    const ctx = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(ctx.task_id).toBe("t1");
    expect(ctx.trace_id).toMatch(/^[0-9a-f]{32}$/);
  });

  it("disconnect 主动取消 → 以 AbortError 结束，状态 CLOSED", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_, init) => {
        // 模拟永不结束的流，等待 abort
        return new Promise<Response>((_, reject) => {
          const timer = setTimeout(() => {}, 100000);
          (init?.signal as AbortSignal)?.addEventListener("abort", () => {
            clearTimeout(timer);
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        });
      }),
    );
    const onStateChange = vi.fn();
    const conn = new SSEConnection({
      taskId: "t1",
      onEvent: () => {},
      onStateChange,
    });
    const p = conn.connect();
    await new Promise((r) => setTimeout(r, 50));
    conn.disconnect();
    // 主动取消不应抛错（catch AbortError）
    await p.catch(() => {});
    expect(conn.state).toBe(SSEConnectionState.CLOSED);
  });
});
