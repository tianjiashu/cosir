import type { BackendSnapshot } from "../../shared/host-contract";
import { logDesktopEvent } from "../desktop-log";
import { HostMethodError, type HostCallMeta, type HostMethodRegistry } from "./methods";

/**
 * 宿主工具链路客户端：宿主主动连后端的 `/host/link`，执行后端发来的方法调用。
 *
 * 帧协议与后端 `app/tools/host/host_link.py` 对应：
 * - 连上后首帧 `{type: "hello", token, host}`，token 是宿主拉起后端时注入的同一个值；
 * - 后端请求 `{id, method, params, meta}`，宿主应答 `{id, result}` 或 `{id, error: {code, message}}`；
 * - 后端通知 `{method, params}` 不应答；`$/cancel` 通知 abort 对应请求的 handler。
 *
 * 连接跟随 supervisor 快照：后端 ready 时连，地址或 generation 变化时换连接，其余状态断开。同一
 * generation 内意外断开就退避重连（后端仍活着，多半是被重启前的短暂抖动）；被 4003 拒绝说明 token
 * 不一致，是装配缺陷，不重连。
 */

const CLOSE_UNAUTHORIZED = 4003;
const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 10_000;
const CANCEL_METHOD = "$/cancel";

export type HostIdentity = { platform: string; pid: number; version: string };

type Target = { generation: number; url: string };

/** 请求 id 只在单条连接内唯一；旧 handler 收尾只能访问它所属连接的登记。 */
type Connection = { socket: WebSocket; inflight: Map<number, AbortController> };

type RequestFrame = { id?: unknown; method?: unknown; params?: unknown; meta?: unknown };

export class HostLinkClient {
  private target: Target | null = null;
  private connection: Connection | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryDelay = INITIAL_RETRY_MS;

  constructor(
    private readonly options: { token: string; host: HostIdentity; methods: HostMethodRegistry },
  ) {}

  /** 每次 supervisor 快照变化时调用。 */
  follow(snapshot: BackendSnapshot): void {
    const url = snapshot.status.state === "ready" && snapshot.backendBaseUrl ? linkUrl(snapshot.backendBaseUrl) : null;
    if (url && this.target?.generation === snapshot.generation && this.target.url === url) return;
    this.disconnect();
    this.target = url ? { generation: snapshot.generation, url } : null;
    if (this.target) {
      this.retryDelay = INITIAL_RETRY_MS;
      this.connect(this.target);
    }
  }

  private connect(target: Target): void {
    const socket = new WebSocket(target.url);
    const connection: Connection = { socket, inflight: new Map() };
    this.connection = connection;
    socket.onopen = () => {
      if (this.connection !== connection) return;
      socket.send(JSON.stringify({ type: "hello", token: this.options.token, host: this.options.host }));
      this.retryDelay = INITIAL_RETRY_MS;
      logDesktopEvent("INFO", "host_link", "host_link_connected", { generation: target.generation });
    };
    socket.onmessage = (event) => {
      if (this.connection === connection) this.receive(connection, event.data);
    };
    socket.onclose = (event) => {
      if (this.connection !== connection) return;
      this.connection = null;
      this.abortInflight(connection);
      if (event.code === CLOSE_UNAUTHORIZED) {
        logDesktopEvent("ERROR", "host_link", "host_link_rejected", { generation: target.generation });
        return;
      }
      logDesktopEvent("INFO", "host_link", "host_link_disconnected", { generation: target.generation, code: event.code });
      if (this.target === target) this.scheduleRetry(target);
    };
  }

  private scheduleRetry(target: Target): void {
    const delay = this.retryDelay;
    this.retryDelay = Math.min(this.retryDelay * 2, MAX_RETRY_MS);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.target === target) this.connect(target);
    }, delay);
  }

  private disconnect(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const connection = this.connection;
    this.connection = null;
    if (connection) {
      this.abortInflight(connection);
      connection.socket.close(1000);
    }
  }

  private abortInflight(connection: Connection): void {
    for (const controller of connection.inflight.values()) controller.abort();
    connection.inflight.clear();
  }

  private receive(connection: Connection, data: unknown): void {
    let frame: RequestFrame;
    try {
      frame = JSON.parse(String(data)) as RequestFrame;
    } catch (error) {
      logDesktopEvent("WARNING", "host_link", "host_link_frame_invalid", {}, error);
      return;
    }
    if (typeof frame.method !== "string") {
      logDesktopEvent("WARNING", "host_link", "host_link_frame_invalid", {});
      return;
    }
    if (frame.method === CANCEL_METHOD) {
      const id = (frame.params as { id?: unknown } | undefined)?.id;
      if (typeof id === "number") connection.inflight.get(id)?.abort();
      return;
    }
    const meta = (frame.meta ?? {}) as HostCallMeta;
    void this.dispatch(connection, typeof frame.id === "number" ? frame.id : null, frame.method, frame.params ?? null, meta);
  }

  /** `id` 为 null 是通知：照常执行，不应答。 */
  private async dispatch({ socket, inflight }: Connection, id: number | null, method: string, params: unknown, meta: HostCallMeta): Promise<void> {
    const handler = this.options.methods.get(method);
    if (!handler) {
      logDesktopEvent("WARNING", "host_link", "host_method_not_found", { method, ...meta });
      if (id !== null) reply(socket, { id, error: { code: "METHOD_NOT_FOUND", message: `unknown host method: ${method}` } });
      return;
    }
    const controller = new AbortController();
    if (id !== null) inflight.set(id, controller);
    try {
      const result = await handler(params, { signal: controller.signal, meta });
      // 已撤销的调用后端不再等待，应答只会被丢弃。
      if (id !== null && !controller.signal.aborted) reply(socket, { id, result: result ?? null });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (id === null) {
        // 通知没有应答可带回失败，后端只知道发出去了：失败只能在这里留下记录（如 Task 删除后回收标签失败）。
        logDesktopEvent(error instanceof HostMethodError ? "WARNING" : "ERROR", "host_link", "host_notification_failed", { method, ...meta }, error);
        return;
      }
      if (error instanceof HostMethodError) {
        reply(socket, { id, error: { code: error.code, message: error.message } });
        return;
      }
      logDesktopEvent("ERROR", "host_link", "host_method_failed", { method, ...meta }, error);
      reply(socket, { id, error: { code: "INTERNAL", message: "the host method failed unexpectedly" } });
    } finally {
      if (id !== null) inflight.delete(id);
    }
  }
}

function linkUrl(backendBaseUrl: string): string {
  return `${backendBaseUrl.replace(/^http/, "ws").replace(/\/$/, "")}/host/link`;
}

function reply(socket: WebSocket, frame: Record<string, unknown>): void {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame));
}
