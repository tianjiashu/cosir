import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { TaskTransportConnection } from "@/lib/assistant/transport/task-transport-connection";
import type { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { activity, transportState, userMessage } from "./transport-fixtures";

// 后端实例由测试控制：连接自己订阅它（不可用时不打开、恢复或换代时重开）。
const backend = vi.hoisted(() => ({
  snapshot: { backendBaseUrl: "http://127.0.0.1:8000", generation: 1, available: true },
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/backend/runtime-config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/backend/runtime-config")>()),
  getBackendRuntimeSnapshot: () => backend.snapshot,
  subscribeBackendRuntime: (listener: () => void) => {
    backend.listeners.add(listener);
    return () => backend.listeners.delete(listener);
  },
}));

function setBackend(change: Partial<typeof backend.snapshot>) {
  backend.snapshot = { ...backend.snapshot, ...change };
  for (const listener of [...backend.listeners]) listener();
}

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

beforeEach(() => {
  // http client 读 window；单测跑在 Node 环境，给一个空 window。宿主动画帧同步执行：每次投影立即发布。
  vi.stubGlobal("window", {});
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
  backend.snapshot = { backendBaseUrl: "http://127.0.0.1:8000", generation: 1, available: true };
  backend.listeners.clear();
});
afterEach(() => vi.unstubAllGlobals());

const fullFrame = (state: TransportState) => ({ task_id: 7, kind: "full", mutations: [], state });

function setup() {
  const fetchImpl = vi.fn<typeof fetch>();
  vi.stubGlobal("fetch", fetchImpl);
  const onStateCommit = vi.fn();
  const connection = new TaskTransportConnection({ taskId: 7, traceId: "trace-7", surface: "conversation", onStateCommit });
  const store = (): TransportFrameStore => {
    const status = connection.getSnapshot().status;
    if (status.kind !== "ready") throw new Error(`connection is ${status.kind}`);
    return status.store;
  };
  return { fetchImpl, connection, store, onStateCommit };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** 打开连接并送达首帧，返回那条流。 */
async function ready(context: ReturnType<typeof setup>, state = transportState()) {
  const stream = controllableStream();
  context.fetchImpl.mockResolvedValueOnce(stream.response);
  context.connection.start();
  await flush();
  stream.push(fullFrame(state));
  await flush();
  return stream;
}

describe("TaskTransportConnection 首帧即初始状态", () => {
  it("首帧到达前是 connecting；首帧建 store 进入 ready，之后的增量交给 store，活动结束不断开", async () => {
    const context = setup();
    const stream = controllableStream();
    context.fetchImpl.mockResolvedValueOnce(stream.response);
    context.connection.start();
    await flush();
    expect(context.connection.getSnapshot().status.kind).toBe("connecting");

    stream.push(fullFrame(transportState({ activity: activity(1, "running") })));
    await flush();
    stream.push({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["activity", "status"], value: "completed" }] });
    await flush();

    expect(context.store().getSnapshot().state.activity?.status).toBe("completed");
    expect(context.onStateCommit.mock.calls.map(([state]) => (state as TransportState).activity?.status)).toEqual(["running", "completed"]);
    expect(context.connection.isOpen).toBe(true);
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);
    expect(context.fetchImpl).toHaveBeenCalledWith(
      expect.stringMatching(/\/tasks\/7\/assistant\/attach$/),
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Accept: "text/event-stream", "X-Trace-Id": "trace-7" }),
      }),
    );
    context.connection.stop();
  });

  it("首帧不是完整快照是协议错误：中止这条连接并按断开重连，不建 store", async () => {
    const context = setup();
    const stream = controllableStream();
    context.fetchImpl.mockResolvedValueOnce(stream.response).mockResolvedValue(controllableStream().response);
    context.connection.start();
    await flush();
    stream.push({ task_id: 7, kind: "mutation", mutations: [] });
    await flush();
    await flush();

    expect(context.fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(context.connection.getSnapshot().status.kind).toBe("connecting");
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    context.connection.stop();
  });
});

describe("TaskTransportConnection 断开与重连", () => {
  it("EOF 交给有限重连：重开订阅，首帧原地对齐", async () => {
    const context = setup();
    const stream = await ready(context);
    const next = controllableStream();
    context.fetchImpl.mockResolvedValueOnce(next.response);
    stream.end();
    await flush();
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    next.push(fullFrame(transportState({ title: "重连后" })));
    await flush();
    expect(context.store().getSnapshot().state.title).toBe("重连后");
    context.connection.stop();
  });

  it("背压重同步立即重开", async () => {
    const context = setup();
    const stream = await ready(context);
    context.fetchImpl.mockResolvedValueOnce(controllableStream().response);
    stream.push({ task_id: 7, kind: "resync_required", mutations: [], resync_reason: "subscriber_backpressure" });
    stream.end();
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    context.connection.stop();
  });

  it("attach 404：Task 已不存在，进入 missing，不再重连", async () => {
    const context = setup();
    context.fetchImpl.mockResolvedValue(new Response(JSON.stringify({ detail: "task not found" }), { status: 404 }));
    context.connection.start();
    await flush();
    await flush();
    expect(context.connection.getSnapshot().status.kind).toBe("missing");
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);
    context.connection.resync();
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("处理帧出错：先中止这条仍然健康的连接，重连不会留下旧连接", async () => {
    const context = setup();
    const stream = await ready(context);
    context.fetchImpl.mockResolvedValue(controllableStream().response);
    stream.push({ task_id: 7, kind: "bogus", mutations: [] });
    await flush();
    expect(context.fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
    context.connection.stop();
  });

  it("后端不可用时关掉订阅；恢复后重开，并提示正在同步", async () => {
    const context = setup();
    await ready(context);
    setBackend({ available: false });
    expect(context.connection.isOpen).toBe(false);
    expect(context.fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);

    context.fetchImpl.mockResolvedValueOnce(controllableStream().response);
    setBackend({ available: true, generation: 2 });
    expect(context.connection.getSnapshot().issue?.message).toContain("正在同步");
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    expect(context.connection.getSnapshot().issue).toBeNull();
    context.connection.stop();
  });

  it("后端不可用时启动不发请求，恢复后才打开", async () => {
    backend.snapshot = { ...backend.snapshot, available: false };
    const context = setup();
    context.fetchImpl.mockResolvedValue(controllableStream().response);
    context.connection.start();
    await flush();
    expect(context.fetchImpl).not.toHaveBeenCalled();
    setBackend({ available: true });
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);
    context.connection.stop();
  });
});

describe("TaskTransportConnection 命令", () => {
  it("提交不重复开订阅：订阅开着（含重连已重开）时不另开", async () => {
    const context = setup();
    const stream = await ready(context);
    const send = vi.fn(async () => undefined);
    await context.connection.submit(command("a"), { send, onError: vi.fn() });
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);

    // 断开后有限重连已重开订阅：此时提交不再另开一条。
    context.fetchImpl.mockResolvedValue(controllableStream().response);
    stream.end();
    await flush();
    await flush();
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    await context.connection.submit(command("b"), { send, onError: vi.fn() });
    expect(context.fetchImpl).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenCalledTimes(2);
    context.connection.stop();
  });

  it("乐观消息保留到 snapshot 出现该用户消息；HTTP 拒绝时收回并交给失败回调", async () => {
    const context = setup();
    const stream = await ready(context);
    await context.connection.submit(command("hello"), { send: async () => undefined, onError: vi.fn() });
    expect(context.store().getSnapshot().pendingCommands).toHaveLength(1);
    stream.push({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["messages", 0], value: userMessage("1", 1, "hello") }] });
    await flush();
    expect(context.store().getSnapshot().pendingCommands).toHaveLength(0);

    const onError = vi.fn();
    const rejection = new Error("bad request");
    const rejected = command("x");
    await context.connection.submit(rejected, { send: async () => { throw rejection; }, onError });
    expect(context.store().getSnapshot().pendingCommands).toHaveLength(0);
    expect(onError).toHaveBeenCalledWith(rejection, [rejected]);
    context.connection.stop();
  });

  it("停止后迟到的成功不重开订阅，迟到的失败仍交给失败回调", async () => {
    const context = setup();
    await ready(context);
    let resolve!: () => void;
    const late = context.connection.submit(command("old"), { send: () => new Promise<void>((yes) => { resolve = yes; }), onError: vi.fn() });
    context.connection.stop();
    resolve();
    await late;
    expect(context.fetchImpl).toHaveBeenCalledTimes(1);

    const onError = vi.fn();
    const rejection = new Error("late");
    await context.connection.submit(command("after stop"), { send: async () => { throw rejection; }, onError });
    expect(onError).toHaveBeenCalledWith(rejection, [expect.objectContaining({ type: "add-message" })]);
  });
});
