import { getApiBaseUrl } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";

/**
 * 每会话一条的可写终端通道（WS `terminal-v2`，契约见 docs/plan/terminal-workbench-design.md 三.6）。
 *
 * 职责：首帧 attach、按 output seq 续传、断线退避重连、resync 时通知调用方清屏、发送 input /
 * resize / release_input。它不持有 xterm，也不判断租约归属（后端仲裁，被拒时收到
 * `input_rejected`）；会话列表与生命周期事实在 Transport snapshot，这里的 `exit` 只用于停止重连
 * 与禁用输入。断线只表示订阅中断，不会关闭会话。
 *
 * 帧里的 `generation` 在会话内固定（后端每个会话运行时生成一次），后端重启后旧会话不会以新
 * generation 重现，而是 attach 时直接得到 `code` 为 `TERMINAL_SESSION_NOT_FOUND` 的 `protocol_error`，
 * 因此这里不跟踪 generation；会话不存在归为 `expired`：停止重连、平静提示，等 snapshot 把它移出列表。
 */

type TerminalServerFrame =
  | {
    type: "attached";
    rows: number;
    columns: number;
    status: string;
    first_available_seq: number;
    next_seq: number;
  }
  | { type: "output"; seq: number; data_base64: string }
  | { type: "geometry"; rows: number; columns: number }
  | { type: "input_mark"; by: "agent" }
  | { type: "input_rejected"; reason: "agent_input" | "session_ended" | "backpressure" }
  /** 只标记流结束；终态、退出码与租约、交接一样由 snapshot `terminal_sessions` 投影。 */
  | { type: "exit" }
  | { type: "resync_required"; first_available_seq?: number; next_seq?: number }
  /** `code` 与 HTTP 错误体同源的稳定码，按它分支；`message` 只用于日志。 */
  | { type: "protocol_error"; code: string; message?: string };

/** 由连接自身消费的帧（output / resync / exit）之外，其余帧原样交给视图。 */
export type TerminalControlFrame = Exclude<TerminalServerFrame, { type: "output" } | { type: "resync_required" } | { type: "exit" }>;

/**
 * - `connecting`：首次连接；`reconnecting`：断线后按退避重连（画面保留，仅头部提示）。
 * - `ended`：会话已结束，不再重连；`expired`：后端已没有该会话（如后端重启），不再重连。
 * - `failed`：其他协议错误或重连耗尽，等用户手动重连。
 */
export type TerminalConnectionState = "connecting" | "connected" | "reconnecting" | "ended" | "expired" | "failed";

export type TerminalConnectionOptions = {
  taskId: number;
  sessionId: string;
  onOutput: (data: Uint8Array) => void;
  /** 服务端缓冲接不上（resync）：调用方清屏，随后从缓冲起点重放。 */
  onReset: () => void;
  onFrame: (frame: TerminalControlFrame) => void;
  onState: (state: TerminalConnectionState) => void;
};

const MAX_RECONNECT_ATTEMPTS = 8;

export function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

export function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function terminalStreamUrl(baseUrl: string, taskId: number, sessionId: string, traceId: TraceId): string {
  const url = new URL(`${baseUrl}/tasks/${taskId}/terminal/sessions/${encodeURIComponent(sessionId)}/stream`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("trace_id", traceId);
  return url.toString();
}

export class TerminalConnection {
  private socket: WebSocket | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryCount = 0;
  private disposed = false;
  private stopped = false;
  /** 已写入 xterm 的最后一个 output seq；0 表示画面为空，需要从缓冲起点 attach。 */
  private lastSeq = 0;
  private state: TerminalConnectionState = "connecting";
  /** 同一会话视图的整条链路共用一个 trace_id，便于在后端日志里串起重连。 */
  private readonly traceId = newTraceId();

  constructor(private readonly options: TerminalConnectionOptions) {}

  start(): void {
    this.connect();
  }

  /** 用户手动重连（⋮ 菜单）：清掉退避计数，从当前 seq 续传。 */
  reconnect(): void {
    if (this.disposed) return;
    this.stopped = false;
    this.retryCount = 0;
    this.clearRetry();
    this.detachSocket(1000, "terminal manual reconnect");
    this.log("INFO", "terminal_ws_manual_reconnect", "用户手动重连终端通道");
    this.connect();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.clearRetry();
    this.detachSocket(1000, "terminal view disposed");
  }

  /** 发送人工输入字节；未连接时返回 false，由视图提示「输入已停用」而不是静默吞键。 */
  sendInput(data: Uint8Array): boolean {
    return this.send({ type: "input", data_base64: encodeBase64(data) });
  }

  sendResize(rows: number, cols: number): boolean {
    return this.send({ type: "resize", rows, cols });
  }

  releaseInput(): boolean {
    return this.send({ type: "release_input" });
  }

  private send(frame: Record<string, unknown>): boolean {
    if (this.state !== "connected" || this.socket === null || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(frame));
    return true;
  }

  private setState(state: TerminalConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.options.onState(state);
  }

  private connect(): void {
    if (this.disposed || this.stopped) return;
    this.setState(this.lastSeq === 0 && this.retryCount === 0 ? "connecting" : "reconnecting");
    const url = terminalStreamUrl(getApiBaseUrl(), this.options.taskId, this.options.sessionId, this.traceId);
    const socket = new WebSocket(url);
    this.socket = socket;
    const afterSeq = this.lastSeq === 0 ? null : this.lastSeq;
    socket.addEventListener("open", () => {
      if (this.socket !== socket) return;
      socket.send(JSON.stringify({ type: "attach", after_seq: afterSeq }));
    });
    socket.addEventListener("message", (message) => this.handleMessage(socket, message.data as string, afterSeq));
    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.scheduleRetry();
    });
  }

  private handleMessage(socket: WebSocket, raw: string, afterSeq: number | null): void {
    if (this.disposed || this.socket !== socket) return;
    let frame: TerminalServerFrame;
    try {
      frame = JSON.parse(raw) as TerminalServerFrame;
    } catch (error) {
      this.log("ERROR", "terminal_ws_frame_invalid", "终端通道收到无法解析的帧", {}, error);
      return;
    }
    if (frame.type === "attached") {
      this.handleAttached(frame, afterSeq);
      return;
    }
    switch (frame.type) {
      case "output":
        if (frame.seq <= this.lastSeq) return;
        if (this.lastSeq !== 0 && frame.seq !== this.lastSeq + 1) {
          this.resync(socket, { gapFrom: this.lastSeq, gapTo: frame.seq });
          return;
        }
        this.lastSeq = frame.seq;
        this.options.onOutput(decodeBase64(frame.data_base64));
        return;
      case "resync_required":
        this.resync(socket, { firstAvailableSeq: frame.first_available_seq ?? null });
        return;
      case "exit":
        this.stopped = true;
        this.setState("ended");
        this.detachSocket(1000, "terminal session exited");
        return;
      case "protocol_error":
        this.stopped = true;
        if (frame.code === "TERMINAL_SESSION_NOT_FOUND") {
          // 会话已不在后端（重启或已淘汰）：不是故障，停止重连并交给 snapshot 收敛列表。
          this.log("INFO", "terminal_ws_session_expired", "终端会话已失效，停止重连");
          this.setState("expired");
        } else {
          this.log("ERROR", "terminal_ws_protocol_error", "终端通道协议错误", { code: frame.code, message: frame.message ?? null });
          this.options.onFrame(frame);
          this.setState("failed");
        }
        this.detachSocket(1000, "terminal protocol error");
        return;
      default:
        this.options.onFrame(frame);
    }
  }

  private handleAttached(frame: Extract<TerminalServerFrame, { type: "attached" }>, afterSeq: number | null): void {
    const resumed = this.retryCount > 0 || afterSeq !== null;
    this.retryCount = 0;
    this.log("INFO", resumed ? "terminal_ws_reconnected" : "terminal_ws_connected", resumed ? "终端通道已续传" : "终端通道已连接", {
      afterSeq,
      firstAvailableSeq: frame.first_available_seq,
      nextSeq: frame.next_seq,
      status: frame.status,
    });
    this.setState("connected");
    this.options.onFrame(frame);
  }

  private resync(socket: WebSocket, data: Record<string, unknown>): void {
    if (socket !== this.socket) return;
    this.lastSeq = 0;
    this.options.onReset();
    this.log("WARNING", "terminal_ws_resync", "终端输出缓冲接不上，清屏重新同步", data);
    this.detachSocket(1000, "terminal resync");
    this.connect();
  }

  private detachSocket(code: number, reason: string): void {
    const socket = this.socket;
    this.socket = null;
    socket?.close(code, reason);
  }

  private scheduleRetry(): void {
    if (this.disposed || this.stopped || this.retryTimer !== null) return;
    if (this.retryCount >= MAX_RECONNECT_ATTEMPTS) {
      this.log("ERROR", "terminal_ws_reconnect_exhausted", "终端通道重连次数耗尽", { attempts: this.retryCount });
      this.setState("failed");
      return;
    }
    const delay = Math.min(5000, 500 * 2 ** this.retryCount);
    this.retryCount += 1;
    this.setState("reconnecting");
    this.log("INFO", "terminal_ws_reconnect_scheduled", "终端通道断开，准备重连", { attempt: this.retryCount, delay, lastSeq: this.lastSeq });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private log(level: "INFO" | "WARNING" | "ERROR", event: string, msg: string, data: Record<string, unknown> = {}, error?: unknown): void {
    void frontendLog(level, event, msg, {
      traceId: this.traceId,
      data: { taskId: this.options.taskId, sessionId: this.options.sessionId, ...data },
      error,
    });
  }
}
