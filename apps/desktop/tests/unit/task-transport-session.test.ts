import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { TaskTransportSession, type TaskTransportSessionCallbacks } from "@/lib/assistant/task-transport-session";
import { TransportFrameStore } from "@/lib/assistant/transport-frame-store";
import { activity, transportState, userMessage } from "./transport-fixtures";

const command = (text: string): UserAddMessageCommand => ({
  type: "add-message",
  commandId: `c-${text}`,
  message: { role: "user", parts: [{ type: "text", text }] },
});

const sse = (frames: unknown[]) => frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");

/** 一个可控的 SSE 响应：push 推帧，end 结束（EOF）。 */
function controllableStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (value) => { controller = value; } });
  const encoder = new TextEncoder();
  return {
    response: new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }),
    push: (frame: unknown) => controller.enqueue(encoder.encode(sse([frame]))),
    end: () => controller.close(),
  };
}

// http client 按 runtime config 解析后端地址，它读 window；单测跑在 Node 环境，给一个空 window。
beforeEach(() => vi.stubGlobal("window", {}));
afterEach(() => vi.unstubAllGlobals());

const fullFrame = (state: TransportState) => ({ task_id: 7, kind: "full", mutations: [], state });

function setup(callbacks: Partial<TaskTransportSessionCallbacks> = {}, initial = transportState()) {
  // 宿主动画帧同步执行：每次投影立即发布。
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
  const store = new TransportFrameStore(initial);
  const fetchImpl = vi.fn<typeof fetch>();
  vi.stubGlobal("fetch", fetchImpl);
  const all: TaskTransportSessionCallbacks = { traceId: "trace-7", ...callbacks };
  const session = new TaskTransportSession(store, "/tasks/7/assistant/attach", () => all);
  return { store, fetchImpl, session };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("TaskTransportSession 长订阅（D4）", () => {
  it("没有活动 Run 时也打开订阅，首帧 full 即完整状态，活动结束不断开", async () => {
    const onDisconnect = vi.fn();
    const { store, fetchImpl, session } = setup({ onDisconnect });
    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);

    void session.open();
    await flush();
    stream.push(fullFrame(transportState({ activity: activity(1, "running") })));
    await flush();
    stream.push({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["activity", "status"], value: "completed" }] });
    await flush();

    expect(store.getSnapshot().state.activity?.status).toBe("completed");
    expect(session.isOpen).toBe(true);
    expect(onDisconnect).not.toHaveBeenCalled();
    expect(fetchImpl).toHaveBeenCalledWith(
      expect.stringMatching(/\/tasks\/7\/assistant\/attach$/),
      expect.objectContaining({
        method: "POST",
        cache: "no-store",
        headers: expect.objectContaining({ Accept: "text/event-stream", "X-Trace-Id": "trace-7" }),
      }),
    );
  });

  it("后端关闭连接（EOF）视为意外断开，交给宿主重连；背压重同步带 resync 标记", async () => {
    const onDisconnect = vi.fn();
    const { fetchImpl, session } = setup({ onDisconnect });
    const eof = controllableStream();
    fetchImpl.mockResolvedValueOnce(eof.response);
    await Promise.all([session.open(), (async () => { await flush(); eof.end(); })()]);
    expect(onDisconnect).toHaveBeenLastCalledWith({ error: null, resync: false });
    expect(session.isOpen).toBe(false);

    const backpressure = controllableStream();
    fetchImpl.mockResolvedValueOnce(backpressure.response);
    await Promise.all([session.open(), (async () => {
      await flush();
      backpressure.push({ task_id: 7, kind: "resync_required", mutations: [], resync_reason: "subscriber_backpressure" });
      backpressure.end();
    })()]);
    expect(onDisconnect).toHaveBeenLastCalledWith({ error: null, resync: true });
  });

  it("attach 非 2xx 作为带错误的断开上报；主动关闭不回调", async () => {
    const onDisconnect = vi.fn();
    const { fetchImpl, session } = setup({ onDisconnect });
    fetchImpl.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { error: { code: "TASK_NOT_FOUND", message: "任务不存在" } } }), { status: 404 }));
    await session.open();
    expect(onDisconnect).toHaveBeenCalledTimes(1);
    expect(onDisconnect.mock.calls[0][0].error).toBeInstanceOf(Error);

    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);
    const opened = session.open();
    await flush();
    session.close();
    // 测试替身的 fetch 不接 AbortSignal：手动结束响应体，模拟真实 fetch 在 abort 后终止读取。
    stream.end();
    await opened;
    expect(onDisconnect).toHaveBeenCalledTimes(1);
  });

  it("命令 202 不决定是否 attach：订阅已开着就不重开，断着才立即补上", async () => {
    const submitMessage = vi.fn(async () => undefined);
    const { fetchImpl, session } = setup({ submitMessage });
    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);
    void session.open();
    await flush();

    await session.submit(command("a"));
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    session.close();
    fetchImpl.mockResolvedValueOnce(controllableStream().response);
    await session.submit(command("b"));
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });
});

describe("TaskTransportSession 乐观命令（D8）", () => {
  it("202 后乐观消息保留，直到 snapshot 出现该用户消息", async () => {
    const { store, fetchImpl, session } = setup({ submitMessage: async () => undefined });
    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);
    void session.open();
    await flush();

    await session.submit(command("hello"));
    expect(store.getSnapshot().pendingCommands).toHaveLength(1);

    stream.push({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["messages", 0], value: userMessage("1", 1, "hello") }] });
    await flush();
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
  });

  it("HTTP 拒绝时收回乐观消息并交给提交错误回调", async () => {
    const onSubmitError = vi.fn();
    const rejection = new Error("bad request");
    const { store, session } = setup({ submitMessage: async () => { throw rejection; }, onSubmitError });
    const pending = command("x");
    await session.submit(pending);
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    expect(onSubmitError).toHaveBeenCalledWith(rejection, [pending]);
  });
});
