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
import { logError, logInfo, logWarn } from "../lib/logger";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { parseSSEFrame } from "./sseParser";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

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
 * SSE 请求 trace 暴露回调。
 *
 * @param traceId - 本次 `/turns/{turn_id}/stream` 请求写入 `x-trace-id` 的客户端 trace。
 * @returns 无。
 *
 * @sideeffect 由调用方决定是否将 traceId 写入 UI 状态；SSE service 自身同时会记录到 conversationTraceStore。
 */
export type SSETraceHandler = (traceId: string) => void;

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
  /** SSE 请求建立前暴露本次请求 trace_id 的回调（可选）。 */
  onTrace?: SSETraceHandler;
  /** 任务 ID（用于日志和错误追踪）。 */
  taskId: string;
  /** 轮次 ID（用于建立 turn 级 SSE 流）。 */
  turnId: string;
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
 *   taskId: "task-uuid",
 *   turnId: "turn-uuid",
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
  /** 是否已收到终态运行事件（run_finished / final_response / run_failed / run_cancelled）。 */
  private _terminalReceived = false;
  /** 是否已通过 disconnect() 主动终止，用于区分"主动取消"与"后端崩溃"。 */
  private _aborted = false;
  /** 本连接生命周期内已见过的 event_id，用于检测单条 SSE 流内后端重复推送同一事件。 */
  private _seenEventIds = new Set<string>();
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
   * 向 GET /turns/{id}/stream 发起 fetch 请求，
   * 通过 ReadableStream 逐步解析 SSE 格式数据，
   * 每收到一个完整事件即调用 onEvent 回调。
   *
   * @throws {Error} 当已在连接中或网络请求失败时抛出。
   *
   * @sideeffect
   * - 发起 HTTP GET 请求到后端 SSE 端点
   * - 持续读取响应体直到流结束或被取消
   * - 触发 onStateChange 回调报告状态变更
   */
  async connect(): Promise<void> {
    if (this._state === SSEConnectionState.CONNECTING || this._state === SSEConnectionState.STREAMING) {
      throw new Error("SSE 连接已在进行中");
    }

    // 重置单次连接生命周期内的判定状态，避免实例复用（或异常路径）残留上一轮的
    // 终态/取消标记，导致真实异常 EOF 被误判为正常结束而吞掉报错。
    this._terminalReceived = false;
    this._aborted = false;
    this._seenEventIds.clear();

    this._abortController = new AbortController();
    this._setState(SSEConnectionState.CONNECTING);
    const path = API_PATHS.TURN_STREAM(this.options.turnId);
    const requestTrace = buildTraceHeaders({ taskId: this.options.taskId });
    this.options.onTrace?.(requestTrace.trace.traceId);
    useConversationTraceStore.getState().recordTrace({
      traceId: requestTrace.trace.traceId,
      taskId: this.options.taskId,
      operation: "turn_stream",
      method: "GET",
      path,
    });
    const requestContext = {
      module: "sse",
      task_id: this.options.taskId,
      turn_id: this.options.turnId,
      method: "GET",
      path,
      trace_id: requestTrace.trace.traceId,
    };

    try {
      const url = `${BASE_URL}${path}`;
      const response = await fetch(url, {
        signal: this._abortController.signal,
        headers: { Accept: "text/event-stream", ...requestTrace.headers },
      });

      recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

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
        // 按双换行切分 SSE 事件
        const events = buffer.split("\n\n");
        // 最后一段可能不完整，保留在 buffer 中
        buffer = events.pop() ?? "";

        for (const eventText of events) {
          const parsed = this.parseSSEEvent(eventText.trim());
          if (parsed) {
            this._markTerminalIfNeeded(parsed);
            // 调试：单条 SSE 流内若同一 event_id 重复出现，说明后端把同一事件推了两遍。
            if (parsed.event_id) {
              if (this._seenEventIds.has(parsed.event_id)) {
                logWarn("sse_stream_dup_event", {
                  module: "sse",
                  event_id: parsed.event_id,
                  event_type: parsed.event_type,
                  task_id: this.options.taskId,
                  turn_id: this.options.turnId,
                });
              } else {
                this._seenEventIds.add(parsed.event_id);
              }
            }
            this.options.onEvent(parsed);
          }
        }
      }

      // 处理 buffer 中可能残留的最后一个事件
      if (buffer.trim()) {
        const parsed = this.parseSSEEvent(buffer.trim());
        if (parsed) {
          this._markTerminalIfNeeded(parsed);
          this.options.onEvent(parsed);
        }
      }

      this._reportStreamEndIfAbnormal(requestContext);
      this._setState(SSEConnectionState.CLOSED);
      logInfo("SSE stream closed", requestContext);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        // 主动取消，不是错误
        this._setState(SSEConnectionState.CLOSED);
        logInfo("SSE connection aborted", requestContext);
      } else {
        logError("SSE 连接失败", err, requestContext);
        this.options.onError?.(err as Error);
        this._setState(SSEConnectionState.CLOSED);
        throw err;
      }
    } finally {
      // 无论正常结束、主动取消还是异常，connect 生命周期结束时统一释放 AbortController，
      // 避免实例上残留已失效的 controller（重连时状态串扰 / 句柄泄漏）。
      // disconnect() 只负责 abort + 标记，不在此处置 null，确保 connect 飞行中
      // 的多次 disconnect 都能通过仍处于存活状态的 controller 兜底中止。
      this._abortController = null;
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
    this._aborted = true;
    // 仅发起 abort；不在此处将 _abortController 置 null。置 null 由 connect() 的
    // finally 分支在生命周期真正结束时统一回收。这样 connect() 仍在飞行中时，
    // 连续的多次 disconnect() 仍可通过仍处于存活状态的 controller 兜底中止，
    // 不会因为首次 disconnect 已置 null 而变成空操作导致无法中止。
    if (this._abortController) {
      this._abortController.abort();
    }
    // 不在此处设置 IDLE 状态，避免与 connect() catch 块产生竞态；
    // connect() 的 AbortError 分支会设置 CLOSED，非连接状态下调用则保持原状态。
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
    const frame = parseSSEFrame(text);
    if (!frame) {
      return null;
    }

    try {
      return JSON.parse(frame.data) as RuntimeEvent;
    } catch (parseErr) {
      logWarn("SSE 事件 JSON 解析失败", {
        module: "sse",
        task_id: this.options.taskId,
        turn_id: this.options.turnId,
        event_type: frame.eventType || "(unknown)",
        data_preview: frame.data.slice(0, 200),
        error: parseErr instanceof Error ? parseErr.message : String(parseErr),
      });
      return null;
    }
  }

  /**
   * 标记是否收到终态运行事件。
   *
   * 识别 run_finished / final_response / run_failed / run_cancelled 四类终态事件，
   * 命中时置位 `_terminalReceived`，供流结束时的异常判定使用。
   *
   * @param event - 待判定的运行时事件。
   *
   * @sideeffect 命中终态事件时置 `_terminalReceived = true`。
   *
   * @private
   */
  private _markTerminalIfNeeded(event: RuntimeEvent): void {
    const terminalTypes = ["run_finished", "final_response", "run_failed", "run_cancelled"];
    if (terminalTypes.includes(event.event_type)) {
      this._terminalReceived = true;
    }
  }

  /**
   * 流异常结束判定与上报。
   *
   * 当流正常读完但既未收到终态事件也非主动取消（disconnect）时，
   * 判定为后端异常结束（如后端崩溃导致流中断），记录 error 日志并回调 onError。
   *
   * @param context - 请求上下文（含 task_id / turn_id / trace_id 等），用于日志定位。
   *
   * @sideeffect 异常时写 error 日志并调用 onError 回调。
   *
   * @private
   */
  private _reportStreamEndIfAbnormal(context: Record<string, unknown>): void {
    if (this._terminalReceived || this._aborted) {
      return;
    }
    const err = new Error("SSE stream ended without terminal event");
    logError("SSE 流异常结束：未收到终态事件，后端可能已崩溃", err, context);
    this.options.onError?.(err);
  }

  /**
   * 更新连接状态并通知观察者。
   *
   * 仅当状态真正变化时才赋值并触发 onStateChange 回调，避免无变化的重复通知。
   *
   * @param newState - 新的连接状态值。
   *
   * @sideeffect 状态变化时调用 onStateChange 回调。
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
