import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { TaskTransportSession, type TaskTransportSessionCallbacks } from "@/lib/assistant/transport/task-transport-session";
import { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { activity, assistantMessage, queuedMessage, toolCallPart, transportState, userMessage } from "../transport-fixtures";

beforeEach(() => vi.stubGlobal("window", {}));
afterEach(() => vi.unstubAllGlobals());
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const command = (text: string): UserAddMessageCommand => ({ type: "add-message", message: { role: "user", parts: [{ type: "text", text }] } });

describe("T1", () => {
  it("帧校验失败：onDisconnect(error)，但旧 controller 未 abort、body 未 cancel", async () => {
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { cb(0); return 0; });
    const store = new TransportFrameStore(transportState());
    let cancelled = false;
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start: (c) => { ctrl = c; }, cancel: () => { cancelled = true; } });
    const signals: AbortSignal[] = [];
    const fetchImpl = vi.fn<typeof fetch>(async (_u, init) => { signals.push(init!.signal!); return new Response(body, { status: 200 }); });
    vi.stubGlobal("fetch", fetchImpl);
    const onDisconnect = vi.fn();
    const cbs: TaskTransportSessionCallbacks = { traceId: "t", onDisconnect };
    const session = new TaskTransportSession(store, 7, () => cbs);
    void session.start();
    await flush();
    ctrl.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ task_id: 7, kind: "bogus", mutations: [] })}\n\n`));
    await flush(); await flush();
    expect(onDisconnect).toHaveBeenCalledWith({ error: expect.any(Error), resync: false });
    expect(String(onDisconnect.mock.calls[0][0].error)).toContain("kind is invalid");
    expect(session.isOpen).toBe(false);
    expect(signals[0].aborted).toBe(false);
    expect(cancelled).toBe(false);
    // 重连不会 abort 旧连接
    fetchImpl.mockImplementationOnce(async (_u, init) => { signals.push(init!.signal!); return new Response(new ReadableStream(), { status: 200 }); });
    void session.open();
    await flush();
    expect(signals[0].aborted).toBe(false);
    expect(cancelled).toBe(false);
    session.close();
    expect(signals[0].aborted).toBe(false);
  });
});

describe("T2", () => {
  it("请求期间到达的 append-text 在页内容已含该段时回放两次", () => {
    const term = (output: string) => toolCallPart("c1", "execute_terminal", { display_data: { kind: "terminal-result", output } } as never);
    const old = assistantMessage("assistant-turn-1", 1, [term("AB")]);
    const store = new TransportFrameStore(transportState({ messages: [userMessage("user-2", 2), assistantMessage("assistant-turn-2", 2)], messageOffset: 2, activity: activity(2, "completed") }));
    const fetch = store.beginHistoryFetch()!;
    store.applyFrame({ task_id: 7, kind: "mutation", mutations: [{ kind: "append-text", path: ["messages", 1, "parts", 0, "display_data", "output"], value: "C" }] });
    // 后端在应用 C 之后才生成页 -> 页里已是 ABC
    const ok = store.completeHistoryFetch(fetch, { messages: [userMessage("user-1", 1), { ...old, parts: [term("ABC")] }], messageOffset: 0 });
    expect(ok).toBe(true);
    const part = store.getSnapshot().state.messages[1].parts[0] as unknown as { display_data: { output: string } };
    expect(part.display_data.output).toBe("ABCC");
  });
});

describe("T4", () => {
  it("full 帧丢弃已翻出的历史", () => {
    const store = new TransportFrameStore(transportState({ messages: [userMessage("user-2", 2)], messageOffset: 1 }));
    const f = store.beginHistoryFetch()!;
    store.completeHistoryFetch(f, { messages: [userMessage("user-1", 1)], messageOffset: 0 });
    expect(store.getSnapshot().state.messages.map((m) => m.id)).toEqual(["user-1", "user-2"]);
    store.applyFrame({ task_id: 7, kind: "full", mutations: [], state: transportState({ messages: [userMessage("user-2", 2)], messageOffset: 1 }) });
    expect(store.getSnapshot().state.messages.map((m) => m.id)).toEqual(["user-2"]);
  });
});

describe("T5", () => {
  it("立即发送换 id 被算作新用户消息，误收在途乐观消息", () => {
    const store = new TransportFrameStore(transportState({ activity: activity(1, "running"), queued_messages: [queuedMessage("user-10", "q")] }));
    const inflight = command("另一条");
    store.setPendingCommands([inflight]);
    // send-now: 原行撤回、新 id 入队
    store.applyFrame({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["queued_messages"], value: [{ ...queuedMessage("user-11", "q"), delivery: "immediate" }] }] });
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    // 在途消息真正入队后才重新出现
    store.applyFrame({ task_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["queued_messages"], value: [{ ...queuedMessage("user-11", "q"), delivery: "immediate" }, queuedMessage("user-12", "另一条")] }] });
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    expect(store.getSnapshot().items.length).toBe(2);
  });
});
