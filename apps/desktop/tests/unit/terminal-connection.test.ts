import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn(async () => undefined) }));

import { frontendLog } from "@/lib/logging/frontend-log";
import {
  decodeBase64,
  encodeBase64,
  TerminalConnection,
  type TerminalConnectionOptions,
  type TerminalConnectionState,
  type TerminalControlFrame,
} from "@/lib/terminal/terminal-connection";

type Listener = (event: { data?: unknown }) => void;

/** 代替浏览器 WebSocket 的替身（经 vi.stubGlobal 注入）；每次构造都登记到 `created`。 */
class FakeSocket {
  static readonly OPEN = 1;
  static created: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closed = false;
  private listeners = new Map<string, Listener[]>();
  constructor(readonly url: string) {
    FakeSocket.created.push(this);
  }
  addEventListener(type: string, listener: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  send(data: string) { this.sent.push(JSON.parse(data) as Record<string, unknown>); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    this.emit("close", {});
  }
  open() {
    this.readyState = 1;
    this.emit("open", {});
  }
  receive(frame: Record<string, unknown>) {
    this.emit("message", { data: JSON.stringify({ protocol: "terminal-v2", session_id: "t1", generation: "g1", ...frame }) });
  }
  /** 模拟网络断开：服务端关闭连接。 */
  drop() {
    this.readyState = 3;
    this.emit("close", {});
  }
  private emit(type: string, event: { data?: unknown }) {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

const ATTACHED = {
  type: "attached", rows: 24, columns: 80, status: "running", first_available_seq: 1, next_seq: 1,
};

function output(seq: number, text: string) {
  return { type: "output", seq, data_base64: encodeBase64(new TextEncoder().encode(text)) };
}

function setup() {
  const sockets = FakeSocket.created;
  const written: string[] = [];
  const frames: TerminalControlFrame[] = [];
  const states: TerminalConnectionState[] = [];
  const resets: string[] = [];
  const options: TerminalConnectionOptions = {
    taskId: 42,
    sessionId: "t1",
    onOutput: (data) => written.push(new TextDecoder().decode(data)),
    onReset: () => resets.push("resync"),
    onFrame: (frame) => frames.push(frame),
    onState: (state) => states.push(state),
  };
  const connection = new TerminalConnection(options);
  connection.start();
  return { connection, sockets, written, frames, states, resets };
}

describe("TerminalConnection（terminal-v2）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.created = [];
    vi.stubGlobal("WebSocket", FakeSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.mocked(frontendLog).mockClear();
  });

  it("首帧 attach，连接后可发送 input / resize / release_input / handoff_done", () => {
    const { connection, sockets, states } = setup();
    expect(sockets[0].url).toMatch(/\/tasks\/42\/terminal\/sessions\/t1\/stream\?trace_id=/);
    expect(connection.sendInput(new Uint8Array([0x6c]))).toBe(false);
    sockets[0].open();
    expect(sockets[0].sent[0]).toEqual({ type: "attach", after_seq: null });
    sockets[0].receive(ATTACHED);
    expect(states.at(-1)).toBe("connected");

    expect(connection.sendInput(new TextEncoder().encode("ls\r"))).toBe(true);
    expect(connection.sendResize(30, 120)).toBe(true);
    connection.releaseInput();
    connection.handoffDone();
    expect(sockets[0].sent.slice(1)).toEqual([
      { type: "input", data_base64: encodeBase64(new TextEncoder().encode("ls\r")) },
      { type: "resize", rows: 30, cols: 120 },
      { type: "release_input" },
      { type: "handoff_done" },
    ]);
    expect(vi.mocked(frontendLog).mock.calls.map((call) => call[1])).toContain("terminal_ws_connected");
  });

  it("断线后按 seq 续传，不清屏", () => {
    const { sockets, written, states, resets } = setup();
    sockets[0].open();
    sockets[0].receive(ATTACHED);
    sockets[0].receive(output(1, "a"));
    sockets[0].receive(output(2, "b"));
    sockets[0].drop();
    expect(states.at(-1)).toBe("reconnecting");

    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    expect(sockets[1].sent[0]).toEqual({ type: "attach", after_seq: 2 });
    sockets[1].receive({ ...ATTACHED, first_available_seq: 1, next_seq: 3 });
    sockets[1].receive(output(2, "dup"));
    sockets[1].receive(output(3, "c"));
    expect(written.join("")).toBe("abc");
    expect(resets).toEqual([]);
    expect(states.at(-1)).toBe("connected");
  });

  it("resync_required 与 seq 断档都会清屏并从缓冲起点重新 attach", () => {
    const { sockets, written, resets } = setup();
    sockets[0].open();
    sockets[0].receive(ATTACHED);
    sockets[0].receive(output(1, "a"));
    sockets[0].receive(output(5, "gap"));
    expect(resets).toEqual(["resync"]);
    expect(sockets[0].closed).toBe(true);
    sockets[1].open();
    expect(sockets[1].sent[0]).toEqual({ type: "attach", after_seq: null });
    sockets[1].receive(ATTACHED);
    sockets[1].receive(output(4, "x"));
    sockets[1].receive({ type: "resync_required", first_available_seq: 9 });
    expect(resets).toEqual(["resync", "resync"]);
    expect(sockets).toHaveLength(3);
    expect(written).toEqual(["a", "x"]);
    expect(vi.mocked(frontendLog).mock.calls.map((call) => call[1])).toContain("terminal_ws_resync");
  });

  it("把输入标记与拒绝帧交给视图；exit 只结束流、不交给视图，之后停止重连", () => {
    const { sockets, frames, states } = setup();
    sockets[0].open();
    sockets[0].receive(ATTACHED);
    sockets[0].receive({ type: "input_mark", by: "agent" });
    sockets[0].receive({ type: "input_rejected", reason: "agent_input" });
    sockets[0].receive({ type: "exit" });
    expect(frames.map((frame) => frame.type)).toEqual(["attached", "input_mark", "input_rejected"]);
    expect(states.at(-1)).toBe("ended");
    vi.advanceTimersByTime(10_000);
    expect(sockets).toHaveLength(1);
  });

  it("协议错误不自动重连，手动重连后恢复", () => {
    const { connection, sockets, states } = setup();
    sockets[0].open();
    sockets[0].receive({ type: "protocol_error", message: "bad attach" });
    expect(states.at(-1)).toBe("failed");
    vi.advanceTimersByTime(10_000);
    expect(sockets).toHaveLength(1);
    connection.reconnect();
    expect(sockets).toHaveLength(2);
  });

  it("会话在后端不存在（如后端重启）时归为 expired：停止重连、不当作协议错误交给视图", () => {
    const { connection, sockets, frames, states } = setup();
    sockets[0].open();
    sockets[0].receive(ATTACHED);
    sockets[0].receive(output(1, "a"));
    sockets[0].drop();
    vi.advanceTimersByTime(500);
    sockets[1].open();
    expect(sockets[1].sent[0]).toEqual({ type: "attach", after_seq: 1 });
    sockets[1].receive({ type: "protocol_error", message: "terminal session not found", generation: undefined });
    expect(states.at(-1)).toBe("expired");
    expect(frames.map((frame) => frame.type)).toEqual(["attached"]);
    expect(sockets[1].closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(2);
    expect(connection.sendInput(new Uint8Array([0x61]))).toBe(false);
    const events = vi.mocked(frontendLog).mock.calls.map((call) => call[1]);
    expect(events).toContain("terminal_ws_session_expired");
    expect(events).not.toContain("terminal_ws_protocol_error");
  });

  it("帧里的 generation 不参与过滤：会话内固定，换值也照常写出", () => {
    const { sockets, written, resets } = setup();
    sockets[0].open();
    sockets[0].receive(ATTACHED);
    sockets[0].receive({ ...output(1, "a"), generation: "other" });
    expect(written).toEqual(["a"]);
    expect(resets).toEqual([]);
  });

  it("base64 编解码往返保持字节不变", () => {
    const bytes = new Uint8Array([0, 3, 27, 255, 128]);
    expect(Array.from(decodeBase64(encodeBase64(bytes)))).toEqual(Array.from(bytes));
  });
});
