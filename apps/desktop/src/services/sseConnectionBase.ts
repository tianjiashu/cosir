/**
 * SSE 连接生命周期通用基类。
 *
 * 单一职责：承载「SSE 连接生命周期」的通用骨架——fetch 发起、Accept 与 trace header 注入、
 * backend trace 记录、ReadableStream 读取循环、eventsource-parser 帧解析接入、EOF flush
 * （feed("\n\n")+reset()）、终态/异常结束判定与 AbortError 分支分流。
 *
 * 不承载任何具体业务帧语义（JSON.parse 后的 RuntimeEvent 处理由子类在 handleFrame 钩子里完成）。
 * 具体的「状态机 vs 布尔」「event_id 去重」「AbortError 兜底语义」差异通过 protected 钩子留白，
 * 由子类各自实现，不在基类中抹平（见重构计划 §3）。
 *
 * 协议解析 100% 走 eventsource-parser（createSSEFrameParser），无手写 split("\n\n") 残留。
 * EOF flush 沿用既有 `feed("\n\n")+reset()` 写法（非官方推荐的 reset({consume:true})），
 * 因为 consume:true 会把不完整残片也当完整帧分发，改变既有语义（重构计划 §1.1 注）。
 *
 * @module services/sseConnectionBase
 */

import type { RuntimeEvent } from "@shared/events";
import { logError, logInfo } from "../lib/logger";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { createSSEFrameParser, type ParsedSSEFrame } from "./sseParser";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/** 默认终态事件类型（父 turn 主 SSE 用 4 类；child 流子类可覆盖为 3 类）。 */
export const TERMINAL_EVENT_TYPES = [
  "run_finished",
  "final_response",
  "run_failed",
  "run_cancelled",
] as const;

/** 终态事件类型（只读元组）。 */
export type TerminalEventType = (typeof TERMINAL_EVENT_TYPES)[number];

/** 子类连接时会透传的上下文（含差异化日志字段）。 */
export interface SSEBaseConnectionContext {
  /** 任务标识，用于 trace、日志定位。 */
  taskId: string;
  /** 模块名（如 "sse" / "delegationStream"），用于日志与 trace operation。 */
  module: string;
  /** 除 taskId/module 外的其他差异化日志字段（如 turn_id / delegation_id / child_turn_id）。 */
  [key: string]: unknown;
}

/** 基类模板方法接受的行为钩子（子类通过构造或调用时传入差异化行为）。 */
export interface SSEBaseHooks {
  /** 连接级错误回调（异常结束 / HTTP 失败时触发）。可选。 */
  onError?: (error: Error) => void;
  /** 请求 trace 暴露回调（父 turn 主 SSE 用；child 流可不传）。可选。 */
  onTrace?: (traceId: string) => void;
}

/**
 * SSE 连接生命周期通用基类。
 *
 * 子类职责：
 * - 实现 `handleFrame`：解析并消费单条已拆分帧（JSON.parse + recordEvent + 去重等）；
 * - 实现 `onAbort`：AbortError 分支的差异化兜底（父类置 CLOSED 并 throw、child 流静默 return）；
 * - 视需在 `isTerminalEvent` / `reportAbnormalEndIfNeeded` 上覆盖终态与异常上报语义。
 *
 * @abstract
 */
export abstract class SSEConnectionBase {
  /** 当前连接的 AbortController；disconnect 仅 abort 不置 null，置 null 由 runConnection 的 finally 回收。 */
  protected _abortController: AbortController | null = null;
  /** 是否已收到终态运行事件。 */
  protected _terminalReceived = false;
  /** 是否已通过 disconnect() 主动终止，用于区分"主动取消"与"后端崩溃"。 */
  protected _aborted = false;

  /**
   * 模板方法：统一 SSE 连接骨架。
   *
   * 负责 fetch + Accept/trace header + backend trace 记录 + ReadableStream 读取循环 +
   * eventsource-parser 帧解析接入 + EOF flush + 终态/异常结束判定 + AbortError 分支分流。
   * 子类只通过 `handleFrame` / `onAbort` 钩子注入差异化逻辑。
   *
   * @param path - 完整 SSE 请求路径（含 BASE_URL 前缀由基类统一拼接）。
   * @param context - 连接上下文（含 taskId/module 及差异化日志字段）。
   * @param hooks - 行为钩子（onError / onTrace 可选）。
   *
   * @throws {Error} HTTP 非 2xx 或读取失败时抛出（AbortError 不向上抛，交由 onAbort 处理）。
   *
   * @sideeffect
   * - 发起 HTTP GET 请求到后端 SSE 端点
   * - 持续读取响应体直到流结束或被取消
   * - 经 hooks.onTrace / conversationTraceStore 记录 trace
   * - finally 中回收 `_abortController` 为 null
   */
  protected async runConnection(path: string, context: SSEBaseConnectionContext, hooks: SSEBaseHooks): Promise<void> {
    this._abortController = new AbortController();
    const requestTrace = buildTraceHeaders({ taskId: context.taskId });
    hooks.onTrace?.(requestTrace.trace.traceId);
    useConversationTraceStore.getState().recordTrace({
      traceId: requestTrace.trace.traceId,
      taskId: context.taskId,
      operation: "turn_stream",
      method: "GET",
      path,
    });
    const requestContext = {
      module: context.module,
      trace_id: requestTrace.trace.traceId,
      method: "GET",
      path,
      ...context,
    };

    try {
      const response = await fetch(`${BASE_URL}${path}`, {
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

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      // 流式帧解析器：内部缓冲拼接分片，完整帧同步回调（替代手写 split("\n\n") 切帧）。
      const frameParser = createSSEFrameParser((frame) => {
        const event = this.parseFrame(frame);
        if (event) {
          if (this.isTerminalEvent(event)) {
            this._terminalReceived = true;
          }
          this.handleFrame(frame);
        }
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
      // 那会把不完整残片也当完整帧分发，改变既有语义。
      frameParser.feed("\n\n");
      frameParser.reset();

      this.reportAbnormalEndIfNeeded(requestContext, hooks);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        // 主动取消分支差异化处理（子类实现：父类置 CLOSED、child 流静默 return）。
        this.onAbort(requestContext);
      } else {
        logError("SSE 连接失败", err, requestContext);
        hooks.onError?.(err as Error);
        throw err;
      }
    } finally {
      // 无论正常结束、主动取消还是异常，连接生命周期结束时统一释放 AbortController，
      // 避免实例上残留已失效的 controller（重连时状态串扰 / 句柄泄漏）。
      // disconnect() 只负责 abort + 标记，不在此处置 null，确保 connect 飞行中
      // 的多次 disconnect 都能通过仍处于存活状态的 controller 兜底中止。
      this._abortController = null;
    }
  }

  /**
   * 帧解析器回调：把已拆分的 SSE 帧解析为 RuntimeEvent。
   *
   * 基类统一做 JSON.parse（失败由子类在 handleFrame 中按需记录，此处返回 null 跳过）；
   * 终态标记由 runConnection 的解析钩子统一处理。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   * @returns 解析成功返回 RuntimeEvent，JSON 格式无效返回 null。
   *
   * @sideeffect 无（解析失败不在此记录，交由子类 handleFrame 决定日志策略）。
   */
  protected parseFrame(frame: ParsedSSEFrame): RuntimeEvent | null {
    try {
      return JSON.parse(frame.data) as RuntimeEvent;
    } catch {
      return null;
    }
  }

  /**
   * 断开 SSE 连接。
   *
   * 标记 `_aborted` 并对仍在存活的 `_abortController` 发起 abort；
   * 置 null 由 runConnection 的 finally 在生命周期真正结束时回收（保持两份现有语义）。
   *
   * @sideeffect 通过 AbortController 中止正在进行的 fetch 请求。
   */
  disconnect(): void {
    this._aborted = true;
    // 仅发起 abort；不在此处将 _abortController 置 null。置 null 由 runConnection 的
    // finally 分支在生命周期真正结束时统一回收。这样 connect() 仍在飞行中时，
    // 连续的多次 disconnect() 仍可通过仍处于存活状态的 controller 兜底中止，
    // 不会因为首次 disconnect 已置 null 而变成空操作导致无法中止。
    this._abortController?.abort();
  }

  /**
   * 判断事件是否为终态运行事件。
   *
   * 默认 4 类（run_finished / final_response / run_failed / run_cancelled）。
   * 子类（如 child 流）可覆盖为 3 类（不含 final_response）以保留语义差异。
   *
   * @param event - 待判定的运行时事件。
   * @returns 命中终态类型时返回 true。
   *
   * @sideeffect 无。
   */
  protected isTerminalEvent(event: RuntimeEvent): boolean {
    return (TERMINAL_EVENT_TYPES as readonly string[]).includes(event.event_type);
  }

  /**
   * 流异常结束判定与上报。
   *
   * 当流正常读完但既未收到终态事件也非主动取消（disconnect）时，
   * 判定为后端异常结束（如后端崩溃导致流中断），记录 error 日志并回调 onError。
   * 子类可覆盖以定制日志文案或上报时机。
   *
   * @param context - 请求上下文（含 task_id / trace_id 等），用于日志定位。
   * @param hooks - 行为钩子（onError 可选）。
   *
   * @sideeffect 异常时写 error 日志并调用 onError 回调。
   */
  protected reportAbnormalEndIfNeeded(context: SSEBaseConnectionContext, hooks: SSEBaseHooks): void {
    if (this._terminalReceived || this._aborted) {
      return;
    }
    const err = new Error("SSE stream ended without terminal event");
    logError("SSE 流异常结束：未收到终态事件，后端可能已崩溃", err, context);
    hooks.onError?.(err);
  }

  /**
   * 帧解析器回调：子类实现 JSON.parse 后的事件消费（recordEvent / 去重等）。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   *
   * @sideeffect 由子类定义（如调用 options.onEvent、更新终态标记、去重告警）。
   *
   * @abstract
   */
  protected abstract handleFrame(frame: ParsedSSEFrame): void;

  /**
   * 主动断开兜底分支：AbortError 时的差异化处理。
   *
   * 父类实现应置 CLOSED 并让外部感知（或 throw）；child 流实现应静默 return，
   * 不影响父 turn（差异语义见重构计划 §3）。
   *
   * @param context - 请求上下文，供子类记录日志。
   *
   * @sideeffect 由子类定义（状态机切换 / 静默 return）。
   *
   * @abstract
   */
  protected abstract onAbort(context: SSEBaseConnectionContext): void;

  /** 暴露给子类的日志辅助：统一 debug 日志前缀。 */
  protected logClosed(context: SSEBaseConnectionContext): void {
    logInfo("SSE stream closed", context);
  }
}
