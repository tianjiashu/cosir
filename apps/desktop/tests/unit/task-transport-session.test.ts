import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { TaskTransportSession, type TaskTransportSessionCallbacks } from "@/lib/assistant/task-transport-session";
import { TransportFrameStore } from "@/lib/assistant/transport-frame-store";
import { activity, transportState, userMessage } from "./transport-fixtures";

const command = (text: string): UserAddMessageCommand => ({
  type: "add-message",
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
  const session = new TaskTransportSession(store, 7, () => all);
  return { store, fetchImpl, session };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("TaskTransportSession 长订阅（D4）", () => {
  it("没有活动 Run 时也打开订阅，首帧 full 即完整状态，活动结束不断开", async () => {
    const onDisconnect = vi.fn();
    const { store, fetchImpl, session } = setup({ onDisconnect });
    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);

    void session.start();
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
    await Promise.all([session.start(), (async () => { await flush(); eof.end(); })()]);
    expect(onDisconnect).toHaveBeenLastCalledWith({ error: null, resync: false });
    expect(session.isOpen).toBe(false);

    const backpressure = controllableStream();
    fetchImpl.mockResolvedValueOnce(backpressure.response);
    await Promise.all([session.start(), (async () => {
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
    await session.start();
    expect(onDisconnect).toHaveBeenCalledTimes(1);
    expect(onDisconnect.mock.calls[0][0].error).toBeInstanceOf(Error);

    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);
    const opened = session.start();
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
    void session.start();
    await flush();

    await session.submit(command("a"));
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    stream.end();
    await flush();
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
    void session.start();
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
    const { store, session, fetchImpl } = setup({ submitMessage: async () => { throw rejection; }, onSubmitError });
    fetchImpl.mockResolvedValueOnce(controllableStream().response);
    void session.start();
    const pending = command("x");
    await session.submit(pending);
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    expect(onSubmitError).toHaveBeenCalledWith(rejection, [pending]);
  });
});


describe("TaskTransportSession owner lifetime", () => {
  for (const failed of [false, true]) it(`ignores late ${failed ? "failure" : "success"} after close, including a new generation`, async () => {
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    const pending = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
    const onSubmitError = vi.fn();
    const { session, fetchImpl } = setup({ submitMessage: () => pending, onSubmitError });
    const stream = controllableStream();
    fetchImpl.mockResolvedValueOnce(stream.response);
    void session.start();
    await flush();
    const submitted = session.submit(command("old"));
    session.close();
    stream.end();
    await session.open(); // stale retry/refetch must not reactivate the session
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const currentStream = controllableStream();
    fetchImpl.mockResolvedValueOnce(currentStream.response);
    void session.start();
    await flush();
    currentStream.end(); // new generation is alive but disconnected
    await flush();
    if (failed) reject(new Error("late")); else resolve();
    await submitted;
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(onSubmitError).not.toHaveBeenCalled();
    session.close();
  });

  it("does not submit a business command after release", async () => {
    const submitMessage = vi.fn();
    const { session } = setup({ submitMessage });
    session.close();
    await expect(session.submit(command("closed"))).rejects.toThrow("释放");
    expect(submitMessage).not.toHaveBeenCalled();
  });
});

it("a reconnect only restores canonical observation after its own full, never from a late history page", async () => {
  const initial = transportState({ activity: activity(7, "completed"), messageOffset: 1, messages: [userMessage("recent", 7)] });
  const invalidated = vi.fn();
  const { session, store, fetchImpl } = setup({ onCanonicalStateInvalidated: invalidated }, initial);
  const first = controllableStream();
  fetchImpl.mockResolvedValueOnce(first.response);
  void session.start();
  await flush();
  expect(session.isOpen).toBe(true);
  expect(session.hasCanonicalState).toBe(false);
  first.push(fullFrame(initial));
  await flush();
  expect(session.hasCanonicalState).toBe(true);
  // EOF 立即失效，随后的旧历史页不能恢复这一资格。
  invalidated.mockClear();
  const history = store.beginHistoryFetch()!;
  first.end(); await flush();
  expect(invalidated).toHaveBeenCalledOnce();
  expect(session.hasCanonicalState).toBe(false);
  const second = controllableStream();
  fetchImpl.mockResolvedValueOnce(second.response);
  void session.open(); await flush();
  expect(store.completeHistoryFetch(history, { messageOffset: 0, messages: [userMessage("old", 1)] })).toBe(true);
  expect(session.hasCanonicalState).toBe(false);
  const eligibility: boolean[] = [];
  const off = store.subscribe(() => eligibility.push(session.hasCanonicalState));
  second.push(fullFrame(transportState({ activity: activity(8, "running") })));
  await flush();
  expect(eligibility).toEqual([true]);
  second.push({ task_id: 7, kind: "resync_required", mutations: [] });
  await flush();
  expect(eligibility).toEqual([true, false]);
  session.close(); second.end(); off();
  expect(session.hasCanonicalState).toBe(false);
});

it("actively replacing a connection invalidates facts before the new HTTP response", async () => {
  const invalidated = vi.fn();
  const { session, fetchImpl } = setup({ onCanonicalStateInvalidated: invalidated });
  const first = controllableStream();
  fetchImpl.mockResolvedValueOnce(first.response);
  void session.start(); await flush();
  first.push(fullFrame(transportState())); await flush();
  expect(session.hasCanonicalState).toBe(true);
  let respond!: (response: Response) => void;
  fetchImpl.mockImplementationOnce(() => new Promise(resolve => { respond = resolve; }));
  invalidated.mockClear();
  void session.open();
  expect(invalidated).toHaveBeenCalledOnce();
  expect(session.hasCanonicalState).toBe(false);
  first.end(); await flush();
  expect(invalidated).toHaveBeenCalledOnce();
  const second = controllableStream();
  respond(second.response); await flush();
  second.push(fullFrame(transportState())); await flush();
  expect(session.hasCanonicalState).toBe(true);
  session.close(); second.end();
});
