import { getApiBaseUrl } from "@/lib/http/client";
import { newTraceId } from "@/lib/trace";

export type TerminalStreamEvent =
  | { type: "attached"; generation?: string | null; status?: string; next_seq?: number }
  | { type: "output"; generation?: string | null; seq: number; data_base64: string }
  | { type: "status"; generation?: string | null; status?: string }
  | { type: "exit"; generation?: string | null; status?: string; exit_code?: number | null; end_reason?: string }
  | { type: "resync_required"; generation?: string | null; first_available_seq?: number; next_seq?: number }
  | { type: "protocol_error"; message?: string };

type TerminalStreamConnectionOptions = {
  taskId: number;
  sessionId: string;
  onEvent: (event: TerminalStreamEvent) => void;
  onState: (state: "connecting" | "connected" | "closed" | "error") => void;
};

/** Return whether an output frame would create a silent cursor gap. */
export function shouldResyncForSequence(
  lastSeq: number,
  nextSeq: number,
  initialized: boolean,
): boolean {
  return initialized && nextSeq !== lastSeq + 1;
}

/** Reject an event from an old backend/session generation. */
export function acceptsTerminalGeneration(
  expected: string | null,
  incoming: string | null | undefined,
): boolean {
  return incoming == null || expected == null || expected === incoming;
}

function toWebSocketUrl(baseUrl: string, taskId: number, sessionId: string): string {
  const url = new URL(`${baseUrl}/tasks/${taskId}/terminal/sessions/${encodeURIComponent(sessionId)}/stream`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("trace_id", newTraceId());
  return url.toString();
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/**
 * 管理一个只读 terminal preview WebSocket。
 *
 * 重连使用 output seq cursor；服务器要求 resync 时清空 xterm 并从 ring buffer
 * 起点重新 attach。任何控制帧都不会由此类发送，唯一发送的是首帧 attach。
 */
export class TerminalStreamConnection {
  private socket: WebSocket | null = null;
  private retryTimer: number | null = null;
  private disposed = false;
  private lastSeq = 0;
  private sequenceInitialized = false;
  private generation: string | null = null;
  private retryCount = 0;

  constructor(private readonly options: TerminalStreamConnectionOptions) {}

  start(): void {
    this.connect(this.lastSeq || null);
  }

  dispose(): void {
    this.disposed = true;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.socket?.close(1000, "terminal preview disposed");
    this.socket = null;
    this.options.onState("closed");
  }

  private connect(afterSeq: number | null): void {
    if (this.disposed) return;
    this.sequenceInitialized = afterSeq !== null;
    this.options.onState("connecting");
    const socket = new WebSocket(toWebSocketUrl(getApiBaseUrl(), this.options.taskId, this.options.sessionId));
    this.socket = socket;
    socket.addEventListener("open", () => {
      if (this.disposed || this.socket !== socket) return;
      this.retryCount = 0;
      socket.send(JSON.stringify({ type: "attach", after_seq: afterSeq }));
      this.options.onState("connected");
    });
    socket.addEventListener("message", (message) => this.handleMessage(socket, message.data));
    socket.addEventListener("error", () => {
      if (!this.disposed && this.socket === socket) this.options.onState("error");
    });
    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (!this.disposed) this.scheduleRetry();
    });
  }

  private handleMessage(socket: WebSocket, raw: unknown): void {
    if (typeof raw !== "string" || this.disposed || this.socket !== socket) return;
    let event: TerminalStreamEvent;
    try {
      event = JSON.parse(raw) as TerminalStreamEvent;
    } catch {
      this.options.onState("error");
      return;
    }
    if (!this.acceptGeneration(event)) return;
    if (event.type === "output") {
      if (!Number.isInteger(event.seq) || event.seq <= this.lastSeq) return;
      if (shouldResyncForSequence(this.lastSeq, event.seq, this.sequenceInitialized)) {
        this.lastSeq = 0;
        this.sequenceInitialized = false;
        this.options.onEvent({ type: "resync_required", next_seq: event.seq });
        socket.close(1013, "terminal output sequence gap");
        return;
      }
      this.lastSeq = event.seq;
      this.sequenceInitialized = true;
      try {
        this.options.onEvent({ ...event, data_base64: event.data_base64 });
      } catch {
        this.options.onState("error");
      }
    } else if (event.type === "resync_required") {
      this.lastSeq = 0;
      this.sequenceInitialized = false;
      this.options.onEvent(event);
      socket.close(1013, "terminal output resync required");
    } else {
      this.options.onEvent(event);
      if (event.type === "exit") socket.close(1000, "terminal session exited");
    }
  }

  private acceptGeneration(event: TerminalStreamEvent): boolean {
    if (event.type === "protocol_error" || event.generation == null) return true;
    if (event.type === "attached") {
      if (this.generation !== null && this.generation !== event.generation) {
        this.lastSeq = 0;
        this.sequenceInitialized = false;
        this.options.onEvent({ type: "resync_required", generation: event.generation });
      }
      this.generation = event.generation;
      return true;
    }
    if (this.generation === null) {
      this.generation = event.generation;
      return true;
    }
    return acceptsTerminalGeneration(this.generation, event.generation);
  }

  /** 供面板消费 output payload，保持解码集中在协议层。 */
  static decodeOutput(event: Extract<TerminalStreamEvent, { type: "output" }>): Uint8Array {
    return decodeBase64(event.data_base64);
  }

  private scheduleRetry(): void {
    if (this.retryTimer !== null || this.disposed) return;
    const delay = Math.min(5000, 500 * 2 ** this.retryCount);
    this.retryCount += 1;
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.connect(this.lastSeq || null);
    }, delay);
  }
}
