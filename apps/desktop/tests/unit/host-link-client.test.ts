import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));

import type { BackendSnapshot } from "@/electron/shared/host-contract";
import { HostLinkClient } from "@/electron/main/host-link/client";
import { HostMethodError, HostMethodRegistry } from "@/electron/main/host-link/methods";

/** 最小的 WebSocket 替身：记录发出的帧，由用例驱动 open / message / close。 */
class FakeSocket {
  static readonly OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closedWith: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code: number): void {
    this.closedWith = code;
    this.readyState = 3;
  }

  open(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  deliver(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  drop(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

const ready = (generation: number, port = 5100): BackendSnapshot => ({
  generation,
  status: { state: "ready" },
  backendBaseUrl: `http://127.0.0.1:${port}`,
});

function client(methods = new HostMethodRegistry()): HostLinkClient {
  return new HostLinkClient({ token: "tok", host: { platform: "test", pid: 1, version: "0.0.0" }, methods });
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("HostLinkClient", () => {
  // 目的：连接跟随后端快照；潜在缺陷：连到旧端口或后端停止后还挂着旧连接。
  it("connects when the backend is ready and follows generation changes", () => {
    const link = client();
    link.follow({ generation: 1, status: { state: "starting" }, backendBaseUrl: null });
    expect(FakeSocket.instances).toHaveLength(0);

    link.follow(ready(1));
    link.follow(ready(1));
    expect(FakeSocket.instances.map((socket) => socket.url)).toEqual(["ws://127.0.0.1:5100/host/link"]);

    link.follow(ready(2, 5200));
    expect(FakeSocket.instances[0].closedWith).toBe(1000);
    expect(FakeSocket.instances[1].url).toBe("ws://127.0.0.1:5200/host/link");

    link.follow({ generation: 3, status: { state: "stopped" }, backendBaseUrl: null });
    expect(FakeSocket.instances[1].closedWith).toBe(1000);
  });

  // 目的：首帧出示 token；潜在缺陷：后端把宿主当作未认证连接拒绝。
  it("sends hello with the token once open", () => {
    const link = client();
    link.follow(ready(1));
    FakeSocket.instances[0].open();
    expect(FakeSocket.instances[0].sent[0]).toEqual({
      type: "hello",
      token: "tok",
      host: { platform: "test", pid: 1, version: "0.0.0" },
    });
  });

  it("dispatches requests and replies with results or error codes", async () => {
    const methods = new HostMethodRegistry();
    methods.register("demo.echo", async (params, { meta }) => ({ params, meta }));
    methods.register("demo.fail", async () => {
      throw new HostMethodError("TAB_NOT_FOUND", "gone");
    });
    methods.register("demo.crash", async () => {
      throw new Error("boom");
    });
    const link = client(methods);
    link.follow(ready(1));
    const socket = FakeSocket.instances[0];
    socket.open();

    socket.deliver({ id: 1, method: "demo.echo", params: { n: 1 }, meta: { task_id: 7 } });
    socket.deliver({ id: 2, method: "demo.fail", params: {} });
    socket.deliver({ id: 3, method: "demo.crash", params: {} });
    socket.deliver({ id: 4, method: "demo.missing", params: {} });
    await settle();

    expect(socket.sent.slice(1)).toEqual(
      expect.arrayContaining([
        { id: 1, result: { params: { n: 1 }, meta: { task_id: 7 } } },
        { id: 2, error: { code: "TAB_NOT_FOUND", message: "gone" } },
        { id: 3, error: { code: "INTERNAL", message: "the host method failed unexpectedly" } },
        { id: 4, error: { code: "METHOD_NOT_FOUND", message: "unknown host method: demo.missing" } },
      ]),
    );
  });

  // 目的：后端撤销调用时 handler 能停下；潜在缺陷：工具已取消，宿主还在执行动作并回一个没人要的应答。
  it("aborts a handler on $/cancel and does not reply", async () => {
    const methods = new HostMethodRegistry();
    let aborted = false;
    methods.register("demo.slow", (_params, { signal }) => new Promise((resolve) => {
      signal.addEventListener("abort", () => {
        aborted = true;
        resolve("late");
      });
    }));
    const link = client(methods);
    link.follow(ready(1));
    const socket = FakeSocket.instances[0];
    socket.open();

    socket.deliver({ id: 9, method: "demo.slow", params: {} });
    socket.deliver({ method: "$/cancel", params: { id: 9 } });
    await settle();

    expect(aborted).toBe(true);
    expect(socket.sent).toHaveLength(1);
  });

  it("runs notifications without replying", async () => {
    const methods = new HostMethodRegistry();
    const received: unknown[] = [];
    methods.register("demo.dropped", async (params) => {
      received.push(params);
    });
    const link = client(methods);
    link.follow(ready(1));
    const socket = FakeSocket.instances[0];
    socket.open();

    socket.deliver({ method: "demo.dropped", params: { task_ids: [1] } });
    await settle();

    expect(received).toEqual([{ task_ids: [1] }]);
    expect(socket.sent).toHaveLength(1);
  });

  // 目的：同一 generation 内断线自动恢复；潜在缺陷：一次抖动后浏览器工具永久不可用。
  it("reconnects with backoff after an unexpected close, but not after a token rejection", () => {
    vi.useFakeTimers();
    const link = client();
    link.follow(ready(1));
    FakeSocket.instances[0].open();

    FakeSocket.instances[0].drop(1006);
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);

    FakeSocket.instances[1].drop(1006);
    vi.advanceTimersByTime(2_000);
    expect(FakeSocket.instances).toHaveLength(3);

    FakeSocket.instances[2].drop(4003);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  // 目的：后端换代后不再重连旧地址；潜在缺陷：旧 generation 的重连计时器连上已退出的端口。
  it("cancels a pending retry when the backend changes", () => {
    vi.useFakeTimers();
    const link = client();
    link.follow(ready(1));
    FakeSocket.instances[0].drop(1006);
    link.follow({ generation: 2, status: { state: "starting" }, backendBaseUrl: null });
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("rejects duplicate method registration", () => {
    const methods = new HostMethodRegistry();
    methods.register("demo.echo", async () => null);
    expect(() => methods.register("demo.echo", async () => null)).toThrow("already registered");
  });
});
