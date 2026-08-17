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
 * 连接生命周期通用骨架（fetch / ReadableStream / AbortController / EOF flush / 终态检测）
 * 已上提到 `SSEConnectionBase`；本类仅保留父 turn 主 SSE 的差异化职责：
 * 4 态状态机 + onStateChange 回调 + 单流内 event_id 去重告警。
 *
 * @module services/sse
 */

import type { RuntimeEvent } from "@shared/events";
import { API_PATHS } from "@shared/api";
import { logInfo, logWarn } from "../lib/logger";
import { SSEConnectionBase, type SSEBaseConnectionContext, type SSEBaseHooks } from "./sseConnectionBase";
import { type ParsedSSEFrame } from "./sseParser";

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
 * 通用连接骨架继承自 SSEConnectionBase；本类维护 4 态状态机、
 * onStateChange 回调、单流内 event_id 去重告警。
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
export class SSEConnection extends SSEConnectionBase {
  private _state: SSEConnectionState = SSEConnectionState.IDLE;
  /** 本连接生命周期内已见过的 event_id，用于检测单条 SSE 流内后端重复推送同一事件。 */
  private _seenEventIds = new Set<string>();
  private readonly options: SSEConnectionOptions;

  constructor(options: SSEConnectionOptions) {
    super();
    this.options = options;
  }

  /** 当前连接状态（只读）。 */
  get state(): SSEConnectionState {
    return this._state;
  }

  /**
   * 建立 SSE 连接并开始接收事件流。
   *
   * 向 GET /turns/{id}/stream 发起 fetch 请求，通过 ReadableStream 逐步解析 SSE 格式数据，
   * 每收到一个完整事件即调用 onEvent 回调。通用 fetch/读取/EOF flush/终态检测由基类
   * runConnection 承担，本方法仅组织 4 态机切换与差异化钩子。
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
    // 状态机与去重集合是本类私有扩展，基类字段（_terminalReceived/_aborted/_abortController）
    // 由基类 runConnection 的 finally 统一重置 controller、此处重置终态/取消标记。
    this._terminalReceived = false;
    this._aborted = false;
    this._seenEventIds.clear();

    this._setState(SSEConnectionState.CONNECTING);

    const path = API_PATHS.TURN_STREAM(this.options.turnId);
    const context: SSEBaseConnectionContext = {
      taskId: this.options.taskId,
      module: "sse",
      turn_id: this.options.turnId,
    };
    const hooks: SSEBaseHooks = {
      onError: this.options.onError,
      onTrace: this.options.onTrace,
    };

    await this.runConnection(path, context, hooks);

    // runConnection 正常 resolve（含 EOF flush + 异常上报）即表示流已结束：置 CLOSED 并记录日志。
    // 主动取消（AbortError）已由 onAbort 钩子置 CLOSED；HTTP/读取异常由 runConnection 抛出，
    // 此处 await 会 reject，不进入该分支（保持原 catch 分支语义）。
    this._setState(SSEConnectionState.CLOSED);
    logInfo("SSE stream closed", context);
  }

  /**
   * 帧解析器回调：解析并分发单条 SSE 帧。
   *
   * 在基类统一 JSON.parse（parseFrame）之后做父 turn 主 SSE 的差异化处理：
   * 单流内 event_id 去重告警 + 调用 options.onEvent。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   *
   * @sideeffect 解析成功时调用 options.onEvent；重复 event_id 时写 warn 日志。
   */
  protected handleFrame(frame: ParsedSSEFrame): void {
    const parsed = this.parseSSEEvent(frame);
    if (parsed) {
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

  /**
   * 主动断开兜底分支：AbortError 视为正常取消，置 CLOSED 并记录日志。
   *
   * @param context - 请求上下文，用于日志定位。
   *
   * @sideeffect 置 _state 为 CLOSED，写 info 日志。
   */
  protected onAbort(context: SSEBaseConnectionContext): void {
    this._setState(SSEConnectionState.CLOSED);
    logInfo("SSE connection aborted", context);
  }

  /**
   * 解析单条 SSE 事件帧。
   *
   * 对已拆分的 SSE 帧做 JSON.parse，构建完整的 RuntimeEvent 对象。
   * 帧的 event 名与 data 字符串已由 createSSEFrameParser 保证非空（缺 event 名的帧不会分发）。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   * @returns 解析成功返回 RuntimeEvent，JSON 格式无效返回 null。
   *
   * @sideeffect JSON 解析失败时写 warn 日志（含定位所需上下文与 data 预览）。
   */
  private parseSSEEvent(frame: ParsedSSEFrame): RuntimeEvent | null {
    try {
      return JSON.parse(frame.data) as RuntimeEvent;
    } catch (parseErr) {
      logWarn("SSE 事件 JSON 解析失败", {
        module: "sse",
        task_id: this.options.taskId,
        turn_id: this.options.turnId,
        event_type: frame.eventType,
        data_preview: frame.data.slice(0, 200),
        error: parseErr instanceof Error ? parseErr.message : String(parseErr),
      });
      return null;
    }
  }

  /**
   * 更新连接状态并通知观察者。
   *
   * 仅当状态真正变化时才赋值并触发 onStateChange 回调，避免无变化的重复通知。
   *
   * @param newState - 新的连接状态值。
   *
   * @sideeffect 状态变化时调用 onStateChange 回调。
   */
  private _setState(newState: SSEConnectionState): void {
    if (this._state !== newState) {
      this._state = newState;
      this.options.onStateChange?.(newState);
    }
  }
}
