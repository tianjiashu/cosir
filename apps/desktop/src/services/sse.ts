/**
 * SSE（Server-Sent Events）流管理。
 *
 * 使用 `fetch + ReadableStream` 连接后端 SSE 端点，
 * 解析 `event:` + `data:` 格式的事件流，并支持：
 * - 流式读取与逐条事件回调
 * - 主动取消连接
 * - 错误处理与状态报告
 *
 * 不使用 EventSource（仅支持 GET 且无法携带自定义 header / 鉴权）。
 *
 * @module services/sse
 */

import type { RuntimeEvent } from "@shared/events";
import { API_PATHS } from "@shared/api";
import { logWarn } from "../lib/logger";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/** SSE 流的连接状态枚举。 */
export enum SSEConnectionState {
  /** 空闲：未连接或已断开 */
  IDLE = "idle",
  /** 正在建立连接 */
  CONNECTING = "connecting",
  /** 已连接且正在接收事件流 */
  STREAMING = "streaming",
  /** 已关闭（正常结束或主动断开） */
  CLOSED = "closed",
}

/** SSE 事件回调类型定义。 */
export type SSEEventHandler = (event: RuntimeEvent) => void;
/** SSE 错误回调类型定义。 */
export type SSEErrorHandler = (error: Error) => void;
/** SSE 状态变更回调类型定义。 */
export type SSEStateChangeHandler = (state: SSEConnectionState) => void;

/**
 * SSE 连接管理器配置选项。
 *
 * @interface SSEConnectionOptions
 */
export interface SSEConnectionOptions {
  /** 收到新事件时的回调（必填）。 */
  onEvent: SSEEventHandler;
  /** 发生错误时的回调（可选）。 */
  onError?: SSEErrorHandler;
  /** 连接状态变更时的回调（可选）。 */
  onStateChange?: SSEStateChangeHandler;
  /** 任务 ID（用于日志和错误追踪）。 */
  taskId: string;
}

/**
 * SSE 连接管理器。
 *
 * 管理 SSE 流的生命周期：连接、接收、错误、关闭。
 * 使用 AbortController 支持主动取消。
 *
 * @example
 * ```ts
 * const conn = new SSEConnection({
 *   taskId: "uuid",
 *   onEvent: (event) => onEventReceived(event),
 *   onStateChange: (state) => onStateChanged(state),
 * });
 * await conn.connect();
 * // ...稍后
 * conn.disconnect();
 * ```
 */
export class SSEConnection {
  private _state: SSEConnectionState = SSEConnectionState.IDLE;
  private _abortController: AbortController | null = null;
  private readonly options: SSEConnectionOptions;

  constructor(options: SSEConnectionOptions) {
    this.options = options;
  }

  /** 当前连接状态（只读）。 */
  get state(): SSEConnectionState {
    return this._state;
  }

  /**
   * 建立 SSE 连接并开始接收事件流。
   *
   * 向 GET /tasks/{id}/stream 发起 fetch 请求，
   * 通过 ReadableStream 逐行解析 SSE 格式数据，
   * 每收到一个完整事件即调用 onEvent 回调。
   *
   * @throws {Error} 当已在连接中或网络请求失败时抛出。
   *
   * @sideeffect
   * - 发起 HTTP GET 请求到后端 SSE 端点
   * - 持续读取响应体直到流结束或被取消
   * - 触发 onStateChange 回调报告状态变化
   */
  async connect(): Promise<void> {
    if (this._state === SSEConnectionState.CONNECTING || this._state === SSEConnectionState.STREAMING) {
      throw new Error("SSE 连接已在进行中");
    }

    this._abortController = new AbortController();
    this._setState(SSEConnectionState.CONNECTING);

    try {
      const url = `${BASE_URL}${API_PATHS.TASK_STREAM(this.options.taskId)}`;
      const response = await fetch(url, {
        signal: this._abortController.signal,
        headers: { Accept: "text/event-stream" },
      });

      if (!response.ok) {
        throw new Error(`SSE 请求失败: HTTP ${response.status} ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error("SSE 响应体为空");
      }

      this._setState(SSEConnectionState.STREAMING);

      // 使用 ReadableStream 读取 SSE 数据
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        // 按双换行分割 SSE 事件
        const events = buffer.split("\n\n");
        // 最后一段可能不完整，保留在 buffer 中
        buffer = events.pop() ?? "";

        for (const eventText of events) {
          const parsed = this.parseSSEEvent(eventText.trim());
          if (parsed) {
            this.options.onEvent(parsed);
          }
        }
      }

      // 处理 buffer 中可能剩余的最后一条事件
      if (buffer.trim()) {
        const parsed = this.parseSSEEvent(buffer.trim());
        if (parsed) {
          this.options.onEvent(parsed);
        }
      }

      this._setState(SSEConnectionState.CLOSED);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        // 主动取消，不是错误
        this._setState(SSEConnectionState.CLOSED);
      } else {
        this.options.onError?.(err as Error);
        this._setState(SSEConnectionState.CLOSED);
        throw err;
      }
    }
  }

  /**
   * 断开 SSE 连接。
   *
   * 调用后 connect() 返回的 Promise 会以 AbortError 结束。
   * 最终状态由 connect() 的 catch(AbortError) 分支设置为 CLOSED，
   * 此方法不设置状态以避免竞态条件。
   *
   * @sideeffect 通过 AbortController 中止正在进行的 fetch 请求。
   */
  disconnect(): void {
    if (this._abortController) {
      this._abortController.abort();
      this._abortController = null;
    }
    // 不在此处设置 IDLE 状态，避免与 connect() catch 块产生竞态
    // connect() 的 AbortError 分支会设置 CLOSED，非连接状态下调用则保持原状态
  }

  /**
   * 解析单条 SSE 事件文本。
   *
   * 从 `event:` 行提取事件类型，从 `data:` 行提取 JSON payload，
   * 构建完整的 RuntimeEvent 对象。
   *
   * @param text - 单条 SSE 事件原始文本（event: + data: 格式）。
   * @returns 解析成功返回 RuntimeEvent，格式无效返回 null。
   *
   * @private
   */
  private parseSSEEvent(text: string): RuntimeEvent | null {
    let eventType = "";
    let dataStr = "";

    for (const line of text.split("\n")) {
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataStr = line.slice(5).trim();
      }
    }

    if (!dataStr || !eventType) {
      return null;
    }

    try {
      return JSON.parse(dataStr) as RuntimeEvent;
    } catch (parseErr) {
      logWarn("SSE 事件 JSON 解析失败", {
        module: "sse",
        taskId: this.options.taskId,
        eventType: eventType || "(unknown)",
        dataPreview: dataStr.slice(0, 200),
        error: parseErr instanceof Error ? parseErr.message : String(parseErr),
      });
      return null;
    }
  }

  /**
   * 更新连接状态并通知观察者。
   *
   * @param newState - 新的连接状态值。
   *
   * @private
   */
  private _setState(newState: SSEConnectionState): void {
    if (this._state !== newState) {
      this._state = newState;
      this.options.onStateChange?.(newState);
    }
  }
}
