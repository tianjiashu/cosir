import type { ThreadMessage } from "@assistant-ui/react";

import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import type {
  TransportError,
  TransportMessage,
  TransportRun,
  TransportState,
} from "@/lib/assistant/contract";
import {
  toPendingUserMessage,
  toSnapshotErrorMessage,
  toThreadMessageWithRenderContext,
  type UserAddMessageCommand,
} from "@/lib/assistant/converter";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";

export type TransportFrameEnvelope = {
  task_id: number;
  kind: "full" | "mutation" | "resync_required";
  mutations: Array<{
    kind: "set" | "append-text";
    path: Array<string | number>;
    value: unknown;
  }>;
  state?: TransportState;
  source_run_id?: number | null;
  current_run_id?: number | null;
  current_run_status?: string | null;
  resync_reason?: string | null;
  target_run_id: number;
  target_run_status?: string | null;
};

export type FrameStoreItem =
  | { kind: "canonical"; message: TransportMessage; context: MessageContext }
  | { kind: "pending"; command: UserAddMessageCommand }
  | { kind: "error"; error: TransportError };

type MessageContext = {
  runId: TransportRun["runId"];
  runStatus: TransportRun["status"];
  endReason: TransportRun["endReason"];
  error: TransportRun["error"];
  isLastRunMessage: boolean;
};

type StoreSnapshot = {
  state: TransportState;
  items: readonly FrameStoreItem[];
  isRunning: boolean;
  targetRunId: number | null;
  targetRunStatus: string | null;
  pendingCommands: readonly UserAddMessageCommand[];
  resyncRequired: boolean;
};

type Listener = () => void;

type ScheduledCommit = {
  cancel: () => void;
};

type PublicationKind = "stream" | "canonical" | "pending";

export type TransportFrameStoreOptions = {
  /** 注入宿主 WebView 的动画帧调度器，生产环境默认使用 requestAnimationFrame。 */
  scheduleCommit?: (callback: (frameTimestamp?: number) => void) => ScheduledCommit;
  /** 注入开发期性能探针；生产构建不传入该能力。 */
  performanceProbe?: AssistantPerformanceProbe;
};

const TERMINAL_STATUSES = new Set(["idle", "completed", "failed", "cancelled", "interrupted"]);

/**
 * 在投影存储接收 SSE frame 前校验并归一化轻量 envelope。
 * 完整 state 仍由 snapshot validator 负责；这里仅负责 frame 元数据和 mutation 形状，
 * 让格式错误在 Transport 边界暴露，不把无效数据带入投影状态。
 */
export function parseTransportFrame(value: unknown): TransportFrameEnvelope {
  if (typeof value !== "object" || value === null) {
    throw new Error("transport frame must be an object");
  }
  const frame = value as Record<string, unknown>;
  if (!Number.isInteger(frame.task_id)) throw new Error("transport frame task_id is invalid");
  if (frame.kind !== "full" && frame.kind !== "mutation" && frame.kind !== "resync_required") {
    throw new Error("transport frame kind is invalid");
  }
  const kind = frame.kind as TransportFrameEnvelope["kind"];
  if (!Array.isArray(frame.mutations)) throw new Error("transport frame mutations are invalid");
  const mutations = frame.mutations.map((value, index) => {
    if (typeof value !== "object" || value === null) {
      throw new Error(`transport frame mutation[${index}] is invalid`);
    }
    const mutation = value as Record<string, unknown>;
    if (mutation.kind !== "set" && mutation.kind !== "append-text") {
      throw new Error(`transport frame mutation[${index}].kind is invalid`);
    }
    if (!Array.isArray(mutation.path) || !mutation.path.every(
      (part) => (typeof part === "string") || (typeof part === "number" && Number.isInteger(part)),
    )) {
      throw new Error(`transport frame mutation[${index}].path is invalid`);
    }
    return {
      kind: mutation.kind as "set" | "append-text",
      path: mutation.path as Array<string | number>,
      value: mutation.value,
    };
  });
  if (!Number.isInteger(frame.target_run_id)) throw new Error("transport frame target_run_id is invalid");
  if (frame.target_run_status !== undefined
    && frame.target_run_status !== null
    && typeof frame.target_run_status !== "string") {
    throw new Error("transport frame target_run_status is invalid");
  }
  if (kind === "full" && frame.state === undefined) {
    throw new Error("full transport frame must include state");
  }
  return {
    task_id: frame.task_id as number,
    kind,
    mutations,
    state: frame.state === undefined ? undefined : parseTransportState(frame.state),
    source_run_id: parseOptionalInteger(frame.source_run_id, "source_run_id"),
    current_run_id: parseOptionalInteger(frame.current_run_id, "current_run_id"),
    current_run_status: parseOptionalString(frame.current_run_status, "current_run_status"),
    resync_reason: parseOptionalString(frame.resync_reason, "resync_reason"),
    target_run_id: frame.target_run_id as number,
    target_run_status: parseOptionalString(frame.target_run_status, "target_run_status"),
  };
}

function parseOptionalInteger(value: unknown, field: string): number | null | undefined {
  if (value !== undefined && value !== null && !Number.isInteger(value)) {
    throw new Error(`transport frame ${field} is invalid`);
  }
  return value as number | null | undefined;
}

function parseOptionalString(value: unknown, field: string): string | null | undefined {
  if (value !== undefined && value !== null && typeof value !== "string") {
    throw new Error(`transport frame ${field} is invalid`);
  }
  return value as string | null | undefined;
}

/**
 * 为单个 task 持有前端的 Transport frame 投影。
 *
 * mutation 只复制路径上的 state 祖先；发生变化的消息得到新的源对象，未变化的历史消息
 * 保持原对象身份，以便 assistant-ui 的按消息订阅复用转换结果。它只是渲染工作副本，
 * 不建立第二个持久化事实源，也不会向 wire state 添加排序或版本字段。流式消息内容在
 * 下一动画帧统一通知订阅者，结构变化和终态则立即通知，保证 active row 不会被同一帧内
 * 的多个 token 重复提交。
 */
export class TransportFrameStore {
  private state: TransportState;
  private items: FrameStoreItem[] = [];
  private pendingCommands: readonly UserAddMessageCommand[] = [];
  private targetRunId: number | null;
  private targetRunStatus: string | null;
  private resyncRequired = false;
  private snapshot: StoreSnapshot;
  private readonly listeners = new Set<Listener>();
  private runItems: FrameStoreItem[][] = [];
  private canonicalItems: FrameStoreItem[] = [];
  private runRanges: Array<{ start: number; end: number }> = [];
  private scheduledCommit: ScheduledCommit | null = null;
  private scheduledFrameTimestamp: number | null = null;
  private terminalConvergenceKey: string | null = null;
  private readonly scheduleCommit: (callback: (frameTimestamp?: number) => void) => ScheduledCommit;
  private readonly performanceProbe: AssistantPerformanceProbe | undefined;

  public constructor(initialState: TransportState, options: TransportFrameStoreOptions = {}) {
    this.state = initialState;
    this.targetRunId = initialState.current_run_id;
    this.targetRunStatus = findRun(initialState, this.targetRunId)?.status ?? null;
    this.scheduleCommit = options.scheduleCommit ?? scheduleWebViewCommit;
    this.performanceProbe = options.performanceProbe;
    this.rebuildAllItems();
    this.snapshot = this.makeSnapshot();
  }

  public subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  public getSnapshot = (): StoreSnapshot => this.snapshot;

  public applyFrame(frame: TransportFrameEnvelope): void {
    const startedAt = this.performanceProbe?.startTiming() ?? null;
    if (this.resyncRequired && frame.kind !== "full") {
      this.recordFrame(frame, startedAt);
      return;
    }
    if (frame.kind === "resync_required") {
      this.cancelScheduledCommit();
      this.resyncRequired = true;
      this.targetRunStatus = frame.target_run_status ?? this.targetRunStatus;
      this.publish();
      this.recordFrame(frame, startedAt);
      return;
    }
    if (frame.kind === "full") {
      if (!frame.state) throw new Error("full transport frame must include state");
      this.cancelScheduledCommit();
      this.state = frame.state;
      this.targetRunId = frame.target_run_id;
      this.targetRunStatus = frame.target_run_status
        ?? findRun(this.state, this.targetRunId)?.status
        ?? null;
      this.resyncRequired = false;
      this.pendingCommands = [];
      this.rebuildAllItems();
      this.publish();
      this.recordFrame(frame, startedAt);
      return;
    }

    const affectedRuns = new Set<number>();
    let rebuildAll = false;
    let structureChanged = false;
    for (const mutation of frame.mutations) {
      if (mutation.path.length === 0) {
        rebuildAll = true;
      }
      if (mutation.path[0] === "runs") {
        if (typeof mutation.path[1] === "number") {
          affectedRuns.add(mutation.path[1]);
          if (!this.runRanges[mutation.path[1]]) structureChanged = true;
        } else {
          rebuildAll = true;
        }
      }
      this.state = applyMutation(this.state, mutation);
    }
    this.targetRunId = frame.target_run_id;
    this.targetRunStatus = frame.target_run_status ?? this.targetRunStatus;
    if (rebuildAll || structureChanged) {
      this.rebuildAllItems();
    } else {
      const changedRuns = [...affectedRuns].sort((left, right) => right - left);
      if (changedRuns.length > 0) this.items = [...this.items];
      for (const runIndex of changedRuns) {
        const run = this.state.runs[runIndex];
        const range = this.runRanges[runIndex];
        if (!run || !range) continue;
        const nextItems = buildRunItems(run, this.runItems[runIndex]);
        this.runItems[runIndex] = nextItems;
        this.canonicalItems.splice(range.start, range.end - range.start, ...nextItems);
        this.items.splice(range.start, range.end - range.start, ...nextItems);
      }
      if (changedRuns.length > 0) this.rebuildRunRanges();
    }
    if (rebuildAll || frame.mutations.some((mutation) => mutation.path[0] === "error")) {
      this.composeItems();
    }
    const canBatchAsActiveRowUpdate = frame.mutations.length > 0
      && frame.mutations.every((mutation) => this.isActiveMessageContentMutation(mutation))
      && !rebuildAll
      && !structureChanged;
    if (!canBatchAsActiveRowUpdate || isTerminalStatus(this.targetRunStatus)) {
      this.publishNow();
    } else {
      this.publishOnNextAnimationFrame();
    }
    this.recordFrame(frame, startedAt);
  }

  public setPendingCommands(commands: readonly UserAddMessageCommand[]): void {
    this.pendingCommands = [...commands];
    this.composeItems();
    this.publish(null, "pending");
  }

  public clearPendingCommands(rejectedCommands?: readonly UserAddMessageCommand[]): void {
    if (this.pendingCommands.length === 0) return;
    if (!rejectedCommands || rejectedCommands.length === 0) {
      this.pendingCommands = [];
    } else {
      const rejectedIds = new Set(
        rejectedCommands
          .map((command) => command.commandId)
          .filter((commandId): commandId is string => typeof commandId === "string"),
      );
      this.pendingCommands = this.pendingCommands.filter((command) => (
        typeof command.commandId === "string"
          ? !rejectedIds.has(command.commandId)
          : !rejectedCommands.includes(command)
      ));
    }
    if (this.pendingCommands.length === 0) {
      this.composeItems();
      this.publish(null, "pending");
      return;
    }
    this.composeItems();
    this.publish(null, "pending");
  }

  public importState(state: TransportState): void {
    this.cancelScheduledCommit();
    this.state = state;
    this.targetRunId = state.current_run_id;
    this.targetRunStatus = findRun(state, this.targetRunId)?.status ?? null;
    this.resyncRequired = false;
    this.rebuildAllItems();
    this.publish();
  }

  /**
   * 立即执行已排队的流式提交。
   *
   * 生产代码通常不需要调用它；它用于终态收敛和确定性测试，确保排队的 active row
   * 更新不会在终态提交之后再次触发一次渲染通知。
   */
  public flushScheduledPublication(): void {
    if (!this.scheduledCommit) return;
    const frameTimestamp = this.scheduledFrameTimestamp;
    this.cancelScheduledCommit();
    this.publish(frameTimestamp, "stream");
  }

  private rebuildAllItems(): void {
    this.runItems = this.state.runs.map((run) => buildRunItems(run));
    this.canonicalItems = [];
    for (const runItems of this.runItems) this.canonicalItems.push(...runItems);
    this.rebuildRunRanges();
    this.composeItems();
  }

  private composeItems(): void {
    const error: FrameStoreItem[] = this.state.error
      ? [{ kind: "error", error: this.state.error }]
      : [];
    const pending: FrameStoreItem[] = this.pendingCommands.map((command) => ({
      kind: "pending",
      command,
    }));
    this.items = [...this.canonicalItems, ...error, ...pending];
  }

  private rebuildRunRanges(): void {
    let start = 0;
    this.runRanges = this.runItems.map((runItems) => {
      const end = start + runItems.length;
      const range = { start, end };
      start = end;
      return range;
    });
  }

  private makeSnapshot(): StoreSnapshot {
    return {
      state: this.state,
      items: this.items,
      isRunning: this.pendingCommands.length > 0
        || (this.targetRunStatus !== null && !TERMINAL_STATUSES.has(this.targetRunStatus)),
      targetRunId: this.targetRunId,
      targetRunStatus: this.targetRunStatus,
      pendingCommands: this.pendingCommands,
      resyncRequired: this.resyncRequired,
    };
  }

  private publish(
    frameTimestamp: number | null = null,
    publicationKind: PublicationKind = "canonical",
  ): void {
    const startedAt = this.performanceProbe?.startTiming() ?? null;
    const terminalKey = isTerminalStatus(this.targetRunStatus)
      ? `${this.targetRunId ?? "none"}:${this.targetRunStatus}`
      : null;
    const isNewTerminalConvergence = terminalKey !== null && terminalKey !== this.terminalConvergenceKey;
    this.terminalConvergenceKey = terminalKey;
    this.snapshot = this.makeSnapshot();
    if (isNewTerminalConvergence) this.performanceProbe?.recordTerminalConvergence();
    if (this.performanceProbe) {
      const activeRun = findRun(this.state, this.targetRunId);
      this.performanceProbe.recordPublication({
        durationMs: this.performanceProbe.finishTiming(startedAt),
        itemCount: this.items.length,
        activeMessageId: activeRun?.messages.at(-1)?.id ?? null,
        publicationKind,
        animationFrameTimestamp: frameTimestamp,
        terminalConvergence: isNewTerminalConvergence,
      });
    }
    // 先登记本次 publication 的 active message 与宿主动画帧，再通知 React 订阅者，
    // 避免异步 commit 落到下一次 publication 的测量上下文。
    for (const listener of this.listeners) listener();
  }

  private recordFrame(frame: TransportFrameEnvelope, startedAt: number | null): void {
    if (!this.performanceProbe) return;
    this.performanceProbe.recordFrame({
      kind: frame.kind,
      mutationCount: frame.mutations.length,
      durationMs: this.performanceProbe.finishTiming(startedAt),
    });
  }

  private publishOnNextAnimationFrame(): void {
    if (this.scheduledCommit) return;
    this.scheduledFrameTimestamp = null;
    this.scheduledCommit = this.scheduleCommit((frameTimestamp) => {
      this.scheduledCommit = null;
      this.scheduledFrameTimestamp = frameTimestamp ?? null;
      this.publish(this.scheduledFrameTimestamp, "stream");
      this.scheduledFrameTimestamp = null;
    });
  }

  private publishNow(): void {
    this.cancelScheduledCommit();
    this.publish();
  }

  private cancelScheduledCommit(): void {
    this.scheduledCommit?.cancel();
    this.scheduledCommit = null;
    this.scheduledFrameTimestamp = null;
  }

  private isActiveMessageContentMutation(
    mutation: TransportFrameEnvelope["mutations"][number],
  ): boolean {
    if (
      mutation.path[0] !== "runs"
      || typeof mutation.path[1] !== "number"
      || mutation.path[2] !== "messages"
      || typeof mutation.path[3] !== "number"
      || mutation.path.length <= 4
    ) return false;

    const run = this.state.runs[mutation.path[1]];
    return run?.runId === this.targetRunId
      && mutation.path[3] === run.messages.length - 1
      && !isTerminalStatus(run.status);
  }
}

function isTerminalStatus(status: string | null): boolean {
  return status !== null && TERMINAL_STATUSES.has(status);
}

function scheduleWebViewCommit(callback: (frameTimestamp?: number) => void): ScheduledCommit {
  if (typeof requestAnimationFrame === "function") {
    const handle = requestAnimationFrame((frameTimestamp) => callback(frameTimestamp));
    return { cancel: () => cancelAnimationFrame(handle) };
  }
  const handle = setTimeout(() => callback(performance.now()), 16);
  return { cancel: () => clearTimeout(handle) };
}

export function convertFrameStoreItem(item: FrameStoreItem): ThreadMessage {
  if (item.kind === "canonical") {
    return toThreadMessageWithRenderContext(item.message, item.context);
  }
  if (item.kind === "pending") {
    const message = toPendingUserMessage(item.command);
    if (!message) throw new Error("pending add-message command has no renderable parts");
    return message;
  }
  return toSnapshotErrorMessage(item.error);
}

function buildRunItems(
  run: TransportRun,
  previous: readonly FrameStoreItem[] = [],
): FrameStoreItem[] {
  const lastAssistantMessageId = [...run.messages].reverse().find((message) => message.role === "assistant")?.id;
  const previousById = new Map(
    previous
      .filter((item): item is Extract<FrameStoreItem, { kind: "canonical" }> => item.kind === "canonical")
      .map((item) => [item.message.id, item]),
  );
  return run.messages.map((message) => {
    const context: MessageContext = {
      runId: run.runId,
      runStatus: run.status,
      endReason: run.endReason,
      error: run.error,
      isLastRunMessage: message.id === lastAssistantMessageId,
    };
    const previousItem = previousById.get(message.id);
    if (previousItem && sameContext(previousItem.context, context) && previousItem.message === message) {
      return previousItem;
    }
    return { kind: "canonical", message, context };
  });
}

function sameContext(left: MessageContext, right: MessageContext): boolean {
  return left.runId === right.runId
    && left.runStatus === right.runStatus
    && left.endReason === right.endReason
    && left.error === right.error
    && left.isLastRunMessage === right.isLastRunMessage;
}

function findRun(state: TransportState, runId: number | null): TransportRun | undefined {
  return runId === null ? undefined : state.runs.find((run) => run.runId === runId);
}

function applyMutation(
  state: TransportState,
  mutation: TransportFrameEnvelope["mutations"][number],
): TransportState {
  if (mutation.path.length === 0) {
    if (mutation.kind !== "set" || typeof mutation.value !== "object" || mutation.value === null) {
      throw new Error("root transport mutation must set an object");
    }
    return mutation.value as TransportState;
  }
  return updateAtPath(state, mutation.path, mutation.kind, mutation.value) as TransportState;
}

function updateAtPath(
  root: unknown,
  path: readonly (string | number)[],
  kind: "set" | "append-text",
  value: unknown,
): unknown {
  const [key, ...rest] = path;
  if (Array.isArray(root)) {
    const next = [...root];
    if (rest.length === 0) {
      if (kind === "append-text") throw new Error("cannot append text to an array");
      if (typeof key !== "number") throw new Error("array mutation requires numeric index");
      if (key === next.length) next.push(value);
      else next[key] = value;
      return next;
    }
    next[key as number] = updateAtPath(next[key as number], rest, kind, value);
    return next;
  }
  if (typeof root !== "object" || root === null) throw new Error("mutation path has no object parent");
  const next = { ...(root as Record<string, unknown>) };
  if (rest.length === 0) {
    if (kind === "append-text") {
      if (typeof next[key as string] !== "string" || typeof value !== "string") {
        throw new Error("append-text mutation requires string target and value");
      }
      next[key as string] += value;
    } else {
      next[key as string] = value;
    }
    return next;
  }
  next[key as string] = updateAtPath(next[key as string], rest, kind, value);
  return next;
}
