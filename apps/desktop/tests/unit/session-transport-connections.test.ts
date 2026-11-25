import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getOrCreateTransportCommandId, type UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { sessionTransportConnections } from "@/lib/assistant/transport/session-transport-connections";
import type { SessionTransportConnection, SessionTransportConnectionOptions } from "@/lib/assistant/transport/session-transport-connection";
import { transportState, userMessage } from "./transport-fixtures";

vi.mock("@/lib/backend/runtime-config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/backend/runtime-config")>()),
  getBackendRuntimeSnapshot: () => ({ backendBaseUrl: "http://127.0.0.1:8000", generation: 1, available: true }),
  subscribeBackendRuntime: () => () => undefined,
}));
const released = vi.hoisted(() => [] as string[]);
vi.mock("@/lib/assistant/attachments/pending-images", () => ({
  releasePendingImages: (parts: readonly { type: string; image?: string }[]) => {
    for (const part of parts) if (part.type === "image") released.push(part.image!);
  },
}));

const imageCommand = (text: string, image: string): UserAddMessageCommand => ({
  type: "add-message",
  message: { role: "user", parts: [{ type: "text", text }, { type: "image", image }] },
});

/** 每次 attach 给一条只推一个完整首帧、之后挂着的流；返回推增量的入口（最近一条流）。 */
function attachStreams() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => undefined;
  vi.stubGlobal("fetch", vi.fn(async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start: (value) => { controller = value; } });
    const send = (frame: unknown) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
    send({ session_id: 7, kind: "full", mutations: [], state: transportState() });
    push = send;
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  }));
  return { push: (frame: unknown) => push(frame) };
}

const options = (sessionId = 7): SessionTransportConnectionOptions => ({ sessionId, traceId: "trace-7", surface: "conversation" });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** 像外壳那样挂载：取连接、登记、等首帧。 */
async function mount(sessionId = 7): Promise<{ connection: SessionTransportConnection; unmount: () => void }> {
  const connection = sessionTransportConnections.connectionFor(options(sessionId));
  const unmount = sessionTransportConnections.retain(connection);
  await flush();
  await flush();
  return { connection, unmount };
}

function pendingCount(connection: SessionTransportConnection): number {
  const status = connection.getSnapshot().status;
  if (status.kind !== "ready") throw new Error(`connection is ${status.kind}`);
  return status.store.getSnapshot().pendingCommands.length;
}

beforeEach(() => {
  vi.stubGlobal("window", {});
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
  released.length = 0;
});
afterEach(() => vi.unstubAllGlobals());

describe("会话级连接登记表", () => {
  it("没有在途提交时，卸载即回收：再次进入是新连接；StrictMode 的卸载重挂不丢登记", async () => {
    attachStreams();
    const first = await mount();
    // StrictMode：effect 清理后立刻重跑，同一个连接对象再次登记、照常工作。
    first.unmount();
    const remount = sessionTransportConnections.retain(first.connection);
    await flush();
    expect(sessionTransportConnections.connectionFor(options())).toBe(first.connection);
    remount();
    expect(sessionTransportConnections.connectionFor(options())).not.toBe(first.connection);
  });

  it("切走时提交还在途：回来拿到同一个连接，乐观消息还在，后发的排在前一条之后", async () => {
    attachStreams();
    const away = await mount();
    const order: string[] = [];
    let finishUpload!: () => void;
    const first = away.connection.submit(imageCommand("first", "blob:first"), {
      send: () => new Promise<void>((resolve) => { finishUpload = () => { order.push("first"); resolve(); }; }),
      onError: vi.fn(),
    });
    away.unmount();

    const back = await mount();
    expect(back.connection).toBe(away.connection);
    expect(pendingCount(back.connection)).toBe(1);
    const second = back.connection.submit(imageCommand("second", "blob:second"), {
      send: async () => { order.push("second"); },
      onError: vi.fn(),
    });
    await flush();
    expect(order).toEqual([]);
    finishUpload();
    await Promise.all([first, second]);
    expect(order).toEqual(["first", "second"]);
    back.unmount();
  });

  it("页面不在时最后一条提交落定即回收连接，并释放已收下命令的图片；被拒的图片随草稿留着", async () => {
    attachStreams();
    const page = await mount();
    let accept!: () => void;
    let reject!: () => void;
    const kept = page.connection.submit(imageCommand("kept", "blob:kept"), { send: () => new Promise<void>((resolve) => { accept = resolve; }), onError: vi.fn() });
    const refused = page.connection.submit(imageCommand("refused", "blob:refused"), {
      send: () => new Promise<void>((_, fail) => { reject = () => fail(new Error("409")); }),
      onError: vi.fn(),
    });
    page.unmount();
    expect(sessionTransportConnections.connectionFor(options())).toBe(page.connection);
    await flush();
    accept();
    await kept;
    await flush();
    // 第二条还在途：不回收，图片也还在乐观消息里（页面回来时要显示）。
    expect(released).toEqual([]);
    await flush();
    reject();
    await refused;
    expect(released).toEqual(["blob:kept"]);
    expect(sessionTransportConnections.connectionFor(options())).not.toBe(page.connection);
  });

  it("页面在时，正式消息替换乐观消息后释放它的图片", async () => {
    const streams = attachStreams();
    const page = await mount();
    const hello = imageCommand("hello", "blob:hello");
    await page.connection.submit(hello, { send: async () => undefined, onError: vi.fn() });
    expect(released).toEqual([]);
    streams.push({ session_id: 7, kind: "mutation", mutations: [{ kind: "set", path: ["messages", 0], value: {
      ...userMessage("1", 1, "hello"), clientMessageId: getOrCreateTransportCommandId(hello),
    } }] });
    await flush();
    expect(pendingCount(page.connection)).toBe(0);
    expect(released).toEqual(["blob:hello"]);
    page.unmount();
  });
});
