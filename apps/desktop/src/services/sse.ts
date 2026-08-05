/**
 * SSE锛圫erver-Sent Events锛夋祦绠＄悊銆?
 *
 * 浣跨敤 `fetch + ReadableStream` 杩炴帴鍚庣 SSE 绔偣锛?
 * 瑙ｆ瀽 `event:` + `data:` 鏍煎紡鐨勪簨浠舵祦锛屽苟鏀寔锛?
 * - 娴佸紡璇诲彇涓庨€愭潯浜嬩欢鍥炶皟
 * - 涓诲姩鍙栨秷杩炴帴
 * - 閿欒澶勭悊涓庣姸鎬佹姤鍛?
 *
 * 涓嶄娇鐢?EventSource锛堜粎鏀寔 GET 涓旀棤娉曟惡甯﹁嚜瀹氫箟 header / 閴存潈锛夈€?
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

/** 鍚庣鍩虹 URL锛屽紑鍙戠幆澧冭蛋 Vite 浠ｇ悊銆?*/
const BASE_URL = "";

/** SSE 娴佺殑杩炴帴鐘舵€佹灇涓俱€?*/
export enum SSEConnectionState {
  /** 绌洪棽锛氭湭杩炴帴鎴栧凡鏂紑 */
  IDLE = "idle",
  /** 姝ｅ湪寤虹珛杩炴帴 */
  CONNECTING = "connecting",
  /** 宸茶繛鎺ヤ笖姝ｅ湪鎺ユ敹浜嬩欢娴?*/
  STREAMING = "streaming",
  /** 宸插叧闂紙姝ｅ父缁撴潫鎴栦富鍔ㄦ柇寮€锛?*/
  CLOSED = "closed",
}

/** SSE 浜嬩欢鍥炶皟绫诲瀷瀹氫箟銆?*/
export type SSEEventHandler = (event: RuntimeEvent) => void;
/** SSE 閿欒鍥炶皟绫诲瀷瀹氫箟銆?*/
export type SSEErrorHandler = (error: Error) => void;
/** SSE 鐘舵€佸彉鏇村洖璋冪被鍨嬪畾涔夈€?*/
export type SSEStateChangeHandler = (state: SSEConnectionState) => void;
/**
 * SSE 璇锋眰 trace 鏆撮湶鍥炶皟銆?
 *
 * @param traceId - 鏈 `/turns/{turn_id}/stream` 璇锋眰鍐欏叆 `x-trace-id` 鐨勫鎴风 trace銆?
 * @returns 鏃犮€?
 *
 * @sideeffect 鐢辫皟鐢ㄦ柟鍐冲畾鏄惁鎶?traceId 鍐欏叆 UI 鐘舵€侊紱SSE service 鑷韩鍚屾椂浼氳褰曞埌 conversationTraceStore銆?
 */
export type SSETraceHandler = (traceId: string) => void;

/**
 * SSE 杩炴帴绠＄悊鍣ㄩ厤缃€夐」銆?
 *
 * @interface SSEConnectionOptions
 */
export interface SSEConnectionOptions {
  /** 鏀跺埌鏂颁簨浠舵椂鐨勫洖璋冿紙蹇呭～锛夈€?*/
  onEvent: SSEEventHandler;
  /** 鍙戠敓閿欒鏃剁殑鍥炶皟锛堝彲閫夛級銆?*/
  onError?: SSEErrorHandler;
  /** 杩炴帴鐘舵€佸彉鏇存椂鐨勫洖璋冿紙鍙€夛級銆?*/
  onStateChange?: SSEStateChangeHandler;
  /** SSE 璇锋眰寤虹珛鍓嶆毚闇叉湰娆¤姹?trace_id 鐨勫洖璋冿紙鍙€夛級銆?*/
  onTrace?: SSETraceHandler;
  /** 浠诲姟 ID锛堢敤浜庢棩蹇楀拰閿欒杩借釜锛夈€?*/
  taskId: string;
  /** 杞 ID锛堢敤浜庡缓绔?turn 绾?SSE 娴侊級銆?*/
  turnId: string;
}

/**
 * SSE 杩炴帴绠＄悊鍣ㄣ€?
 *
 * 绠＄悊 SSE 娴佺殑鐢熷懡鍛ㄦ湡锛氳繛鎺ャ€佹帴鏀躲€侀敊璇€佸叧闂€?
 * 浣跨敤 AbortController 鏀寔涓诲姩鍙栨秷銆?
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
 * // ...绋嶅悗
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

  /** 褰撳墠杩炴帴鐘舵€侊紙鍙锛夈€?*/
  get state(): SSEConnectionState {
    return this._state;
  }

  /**
   * 寤虹珛 SSE 杩炴帴骞跺紑濮嬫帴鏀朵簨浠舵祦銆?
   *
   * 鍚?GET /turns/{id}/stream 鍙戣捣 fetch 璇锋眰锛?
   * 閫氳繃 ReadableStream 閫愯瑙ｆ瀽 SSE 鏍煎紡鏁版嵁锛?
   * 姣忔敹鍒颁竴涓畬鏁翠簨浠跺嵆璋冪敤 onEvent 鍥炶皟銆?
   *
   * @throws {Error} 褰撳凡鍦ㄨ繛鎺ヤ腑鎴栫綉缁滆姹傚け璐ユ椂鎶涘嚭銆?
   *
   * @sideeffect
   * - 鍙戣捣 HTTP GET 璇锋眰鍒板悗绔?SSE 绔偣
   * - 鎸佺画璇诲彇鍝嶅簲浣撶洿鍒版祦缁撴潫鎴栬鍙栨秷
   * - 瑙﹀彂 onStateChange 鍥炶皟鎶ュ憡鐘舵€佸彉鍖?
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
        throw new Error(`SSE 璇锋眰澶辫触: HTTP ${response.status} ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error("SSE 响应体为空");
      }

      this._setState(SSEConnectionState.STREAMING);

      // 浣跨敤 ReadableStream 璇诲彇 SSE 鏁版嵁
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        // 鎸夊弻鎹㈣鍒嗗壊 SSE 浜嬩欢
        const events = buffer.split("\n\n");
        // 鏈€鍚庝竴娈靛彲鑳戒笉瀹屾暣锛屼繚鐣欏湪 buffer 涓?
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

      // 澶勭悊 buffer 涓彲鑳藉墿浣欑殑鏈€鍚庝竴鏉′簨浠?
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
        // 涓诲姩鍙栨秷锛屼笉鏄敊璇?
        this._setState(SSEConnectionState.CLOSED);
        logInfo("SSE connection aborted", requestContext);
      } else {
        logError("SSE 杩炴帴澶辫触", err, requestContext);
        this.options.onError?.(err as Error);
        this._setState(SSEConnectionState.CLOSED);
        throw err;
      }
    }
  }

  /**
   * 鏂紑 SSE 杩炴帴銆?
   *
   * 璋冪敤鍚?connect() 杩斿洖鐨?Promise 浼氫互 AbortError 缁撴潫銆?
   * 鏈€缁堢姸鎬佺敱 connect() 鐨?catch(AbortError) 鍒嗘敮璁剧疆涓?CLOSED锛?
   * 姝ゆ柟娉曚笉璁剧疆鐘舵€佷互閬垮厤绔炴€佹潯浠躲€?
   *
   * @sideeffect 閫氳繃 AbortController 涓姝ｅ湪杩涜鐨?fetch 璇锋眰銆?
   */
  disconnect(): void {
    this._aborted = true;
    if (this._abortController) {
      this._abortController.abort();
      this._abortController = null;
    }
    // 涓嶅湪姝ゅ璁剧疆 IDLE 鐘舵€侊紝閬垮厤涓?connect() catch 鍧椾骇鐢熺珵鎬?
    // connect() 鐨?AbortError 鍒嗘敮浼氳缃?CLOSED锛岄潪杩炴帴鐘舵€佷笅璋冪敤鍒欎繚鎸佸師鐘舵€?
  }

  /**
   * 瑙ｆ瀽鍗曟潯 SSE 浜嬩欢鏂囨湰銆?
   *
   * 浠?`event:` 琛屾彁鍙栦簨浠剁被鍨嬶紝浠?`data:` 琛屾彁鍙?JSON payload锛?
   * 鏋勫缓瀹屾暣鐨?RuntimeEvent 瀵硅薄銆?
   *
   * @param text - 鍗曟潯 SSE 浜嬩欢鍘熷鏂囨湰锛坋vent: + data: 鏍煎紡锛夈€?
   * @returns 瑙ｆ瀽鎴愬姛杩斿洖 RuntimeEvent锛屾牸寮忔棤鏁堣繑鍥?null銆?
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
      logWarn("SSE 浜嬩欢 JSON 瑙ｆ瀽澶辫触", {
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
   * 鏇存柊杩炴帴鐘舵€佸苟閫氱煡瑙傚療鑰呫€?
   *
   * @param newState - 鏂扮殑杩炴帴鐘舵€佸€笺€?
   *
   * @private
   */
  private _markTerminalIfNeeded(event: RuntimeEvent): void {
    const terminalTypes = ["run_finished", "final_response", "run_failed", "run_cancelled"];
    if (terminalTypes.includes(event.event_type)) {
      this._terminalReceived = true;
    }
  }

  private _reportStreamEndIfAbnormal(context: Record<string, unknown>): void {
    if (this._terminalReceived || this._aborted) {
      return;
    }
    const err = new Error("SSE stream ended without terminal event");
    logError("SSE 流异常结束：未收到终态事件，后端可能已崩溃", err, context);
    this.options.onError?.(err);
  }

  private _setState(newState: SSEConnectionState): void {
    if (this._state !== newState) {
      this._state = newState;
      this.options.onStateChange?.(newState);
    }
  }
}
