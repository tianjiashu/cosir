/**
 * 委派 child turn 的只订阅 SSE 连接。
 *
 * 使用后端 `/turns/{child_turn_id}/events/stream` subscribe-only 端点读取子 turn
 * 运行时事件。该连接不认领 turn、不启动 Agent、不写全局父 SSE 连接状态；调用方通过
 * 回调把事件合并进同一个 eventStore。
 *
 * @module services/delegationStream
 */

import type { RuntimeEvent } from "@shared/events";
import { logError, logInfo, logWarn } from "@/lib/logger";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { createSSEFrameParser, type ParsedSSEFrame } from "./sseParser";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/** child stream 收到运行时事件时的回调。 */
export type DelegationStreamEventHandler = (event: RuntimeEvent) => void;

/** child stream 失败时的回调。 */
export type DelegationStreamErrorHandler = (error: Error) => void;

/**
 * child stream 连接选项。
 */
export interface DelegationStreamConnectionOptions {
  /** 父任务标识，用于 trace、日志和事件回填。 */
  taskId: string;
  /** 委派记录标识，用于日志定位。 */
  delegationId: string;
  /** 被委派 child turn 标识。 */
  childTurnId: string;
  /** 收到 RuntimeEvent 后调用。 */
  onEvent: DelegationStreamEventHandler;
  /** 连接失败、异常结束或无终态关闭时调用。 */
  onError?: DelegationStreamErrorHandler;
}

/**
 * 构造 child turn subscribe-only SSE 路径。
 *
 * @param childTurnId - child turn 标识。
 * @returns 后端 subscribe-only SSE 路径。
 *
 * @sideeffect 无。
 */
function childTurnEventsStreamPath(childTurnId: string): string {
  return `/turns/${encodeURIComponent(childTurnId)}/events/stream`;
}

/**
 * 判断事件是否是 child run 终态。
 *
 * @param event - 待判定运行时事件。
 * @returns 命中 run_finished / run_failed / run_cancelled 时返回 true。
 *
 * @sideeffect 无。
 */
function isChildTerminalEvent(event: RuntimeEvent): boolean {
  return ["run_finished", "run_failed", "run_cancelled"].includes(event.event_type);
}

/**
 * 委派 child turn 只订阅 SSE 连接。
 *
 * 单个实例只管理一个 child turn 的一次连接生命周期。实例状态保持私有，
 * 不读写 `eventStore.connectionState`，避免 child stream 错误污染父 turn 主 SSE 状态。
 */
export class DelegationStreamConnection {
  private readonly options: DelegationStreamConnectionOptions;
  private abortController: AbortController | null = null;
  private terminalReceived = false;
  private aborted = false;
  private connecting = false;

  constructor(options: DelegationStreamConnectionOptions) {
    this.options = options;
  }

  /**
   * 建立 child turn subscribe-only SSE 连接。
   *
   * @returns 连接生命周期 Promise；流结束、主动断开或异常后 resolve/reject。
   * @throws {Error} 当同一实例已在连接中、HTTP 失败或读取失败时抛出。
   *
   * @sideeffect 发起 GET `/turns/{child_turn_id}/events/stream`，持续读取事件并触发回调。
   */
  async connect(): Promise<void> {
    if (this.connecting) {
      throw new Error("delegation child stream is already connecting");
    }

    this.abortController = new AbortController();
    this.terminalReceived = false;
    this.aborted = false;
    this.connecting = true;

    const path = childTurnEventsStreamPath(this.options.childTurnId);
    const requestTrace = buildTraceHeaders({ taskId: this.options.taskId });
    const requestContext = {
      module: "delegationStream",
      task_id: this.options.taskId,
      delegation_id: this.options.delegationId,
      child_turn_id: this.options.childTurnId,
      method: "GET",
      path,
      trace_id: requestTrace.trace.traceId,
    };

    useConversationTraceStore.getState().recordTrace({
      traceId: requestTrace.trace.traceId,
      taskId: this.options.taskId,
      operation: "turn_stream",
      method: "GET",
      path,
    });

    try {
      const response = await fetch(`${BASE_URL}${path}`, {
        signal: this.abortController.signal,
        headers: { Accept: "text/event-stream", ...requestTrace.headers },
      });

      recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

      if (!response.ok) {
        throw new Error(`delegation child stream failed: HTTP ${response.status} ${response.statusText}`);
      }
      if (!response.body) {
        throw new Error("delegation child stream response body is empty");
      }

      await this.readEvents(response.body);
      this.reportAbnormalEndIfNeeded(requestContext);
      logInfo("delegation child stream closed", requestContext);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        logInfo("delegation child stream aborted", requestContext);
        return;
      }
      const error = err instanceof Error ? err : new Error(String(err));
      logError("delegation child stream failed", error, requestContext);
      this.options.onError?.(error);
      throw error;
    } finally {
      this.connecting = false;
      this.abortController = null;
    }
  }

  /**
   * 主动断开 child stream。
   *
   * @sideeffect 通过 AbortController 中止正在进行的 fetch/read。
   */
  disconnect(): void {
    this.aborted = true;
    this.abortController?.abort();
  }

  /**
   * 读取并解析 SSE 响应体。
   *
   * @param body - fetch 返回的 ReadableStream。
   * @returns 读取完成后 resolve。
   *
   * @sideeffect 每解析出一条 RuntimeEvent 即调用 `options.onEvent`。
   */
  private async readEvents(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    // 流式帧解析器：内部缓冲拼接分片，完整帧同步回调（替代手写 split("\n\n") 切帧）
    const frameParser = createSSEFrameParser((frame) => {
      this.handleFrame(frame);
    });

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      frameParser.feed(decoder.decode(value, { stream: true }));
    }

    // EOF：喂入终止空行促使缓冲区内已完整帧被分发（模拟标准流终止），
    // 随后 reset 释放解析器内部状态。不要用 reset({ consume: true })，
    // 那会把不完整残片也当完整帧分发，改变语义。
    frameParser.feed("\n\n");
    frameParser.reset();
  }

  /**
   * 解析并分发单条 SSE 帧。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   *
   * @sideeffect 解析成功时调用 `onEvent`；终态事件会更新实例终态标记。
   */
  private handleFrame(frame: ParsedSSEFrame): void {
    const event = this.parseRuntimeEvent(frame);
    if (!event) {
      return;
    }
    if (isChildTerminalEvent(event)) {
      this.terminalReceived = true;
    }
    this.options.onEvent(event);
  }

  /**
   * 把 SSE 帧解析为 RuntimeEvent。
   *
   * 帧的 event 名与 data 字符串已由 createSSEFrameParser 保证非空（缺 event 名的帧不会分发）。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   * @returns 解析成功的 RuntimeEvent；JSON 无效时返回 null。
   *
   * @sideeffect JSON 解析失败时写 warn 日志（含定位所需上下文与 data 预览）。
   */
  private parseRuntimeEvent(frame: ParsedSSEFrame): RuntimeEvent | null {
    try {
      return JSON.parse(frame.data) as RuntimeEvent;
    } catch (err) {
      logWarn("delegation child stream event parse failed", {
        module: "delegationStream",
        task_id: this.options.taskId,
        delegation_id: this.options.delegationId,
        child_turn_id: this.options.childTurnId,
        event_type: frame.eventType,
        data_preview: frame.data.slice(0, 200),
        error: err instanceof Error ? err.message : String(err),
      });
      return null;
    }
  }

  /**
   * 订阅流正常 EOF 但没有收到 child 终态时，上报异常结束。
   *
   * @param context - 日志上下文。
   *
   * @sideeffect 异常结束时写 error 日志并触发 `onError`，供 hook 强制 backfill。
   */
  private reportAbnormalEndIfNeeded(context: Record<string, unknown>): void {
    if (this.terminalReceived || this.aborted) {
      return;
    }
    const error = new Error("delegation child stream ended without terminal event");
    logError("delegation child stream ended without terminal event", error, context);
    this.options.onError?.(error);
  }
}
