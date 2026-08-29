/**
 * 委派 child turn 的只订阅 SSE 连接。
 *
 * 使用后端 `/turns/{child_turn_id}/events/stream` subscribe-only 端点读取子 turn
 * 运行时事件。该连接不认领 turn、不启动 Agent、不写全局父 SSE 连接状态；调用方通过
 * 回调把事件合并进同一个 eventStore。
 *
 * 连接生命周期通用骨架已上提到 SSEConnectionBase；本类保留 child 流的差异化职责：
 * - 只订阅端点路径与差异化日志字段（delegation_id / child_turn_id）
 * - 终态判定仅 3 类（run_finished / run_failed / run_cancelled，不含 final_response）
 * - AbortError 静默 return（不污染父 turn 主 SSE 状态）
 * - connecting 布尔防重入（不引入状态机）
 *
 * @module services/delegationStream
 */

import type { RuntimeEvent } from "@shared/events";
import { logInfo, logWarn } from "@/lib/logger";
import { SSEConnectionBase, type SSEBaseConnectionContext, type SSEBaseHooks } from "./sseConnectionBase";
import { type ParsedSSEFrame } from "./sseParser";

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
  /** 委派记录标识（真实后端 delegation 主键，number 维度），用于日志定位。 */
  delegationId: number;
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
 * 委派 child turn 只订阅 SSE 连接。
 *
 * 单个实例只管理一个 child turn 的一次连接生命周期。实例状态保持私有，
 * 不读写 `eventStore.connectionState`，避免 child stream 错误污染父 turn 主 SSE 状态。
 * 通用连接骨架继承自 SSEConnectionBase；本类维护 child 流的终态判定（3 类）、
 * connecting 防重入布尔与差异化日志字段。
 */
export class DelegationStreamConnection extends SSEConnectionBase {
  private readonly options: DelegationStreamConnectionOptions;
  /** 防重入布尔（替代状态机；child 流无 CONNECTING/STREAMING 细分，无 onStateChange 回调）。 */
  private connecting = false;

  constructor(options: DelegationStreamConnectionOptions) {
    super();
    this.options = options;
  }

  /**
   * 建立 child turn subscribe-only SSE 连接。
   *
   * 通用 fetch/读取/EOF flush/终态检测由基类 runConnection 承担，本方法仅组织
   * connecting 防重入与差异化连接上下文（delegation_id / child_turn_id）。
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

    this.connecting = true;

    const path = childTurnEventsStreamPath(this.options.childTurnId);
    const context: SSEBaseConnectionContext = {
      taskId: this.options.taskId,
      module: "delegationStream",
      delegation_id: this.options.delegationId,
      child_turn_id: this.options.childTurnId,
    };
    const hooks: SSEBaseHooks = {
      // child 流不在基类层暴露 onTrace（与原有实现一致：无 onTrace 暴露）。
      onError: this.options.onError,
    };

    try {
      await this.runConnection(path, context, hooks);
      // runConnection 正常 resolve（含 EOF flush + 异常上报）即流已结束。
      logInfo("delegation child stream closed", context);
    } finally {
      this.connecting = false;
    }
  }

  /**
   * 帧解析器回调：解析并分发单条 SSE 帧。
   *
   * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
   *
   * @sideeffect 解析成功时调用 options.onEvent；JSON 无效时写 warn 日志。
   */
  protected handleFrame(frame: ParsedSSEFrame): void {
    const event = this.parseRuntimeEvent(frame);
    if (!event) {
      return;
    }
    this.options.onEvent(event);
  }

  /**
   * 判断事件是否是 child run 终态。
   *
   * child 流终态仅 3 类（run_finished / run_failed / run_cancelled），
   * 不含 final_response：child turn 的 final_response 不是 child run 结束信号，
   * run_finished 才是，统一为 4 类会导致 child 流过早误判终态而漏收滞后事件。
   * 覆盖基类默认 4 类，保留 child 流语义差异（与重构计划 §3「不强行统一」一致）。
   *
   * @param event - 待判定运行时事件。
   * @returns 命中 run_finished / run_failed / run_cancelled 时返回 true。
   *
   * @sideeffect 无。
   */
  protected isTerminalEvent(event: RuntimeEvent): boolean {
    return ["run_finished", "run_failed", "run_cancelled"].includes(event.event_type);
  }

  /**
   * 主动断开兜底分支：AbortError 静默 return。
   *
   * child 流断开不影响父 turn 主 SSE 状态，不置状态、不抛。
   *
   * @param context - 请求上下文，用于日志定位。
   *
   * @sideeffect 写 info 日志（便于排查断连），不修改任何全局连接状态。
   */
  protected onAbort(context: SSEBaseConnectionContext): void {
    logInfo("delegation child stream aborted", context);
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
}
