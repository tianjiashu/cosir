import type { ThreadMessage } from "@assistant-ui/react";

import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import {
  isActiveRunStatus,
  isTerminalRunStatus,
  type TransportHistoryPage,
  type TransportMessage,
  type TransportQueuedMessage,
  type TransportRunStatus,
  type TransportState,
} from "@/lib/assistant/contract";
import {
  toPendingUserMessage,
  toThreadMessageWithRenderContext,
  type TransportMessageRenderContext,
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
  resync_reason?: string | null;
};

type TransportMutation = TransportFrameEnvelope["mutations"][number];

/** 一次往上翻页：`end` 是请求时的 `messageOffset`，`generation` 用来认出期间被完整快照替换的窗口。 */
export type HistoryFetch = { generation: number; end: number };

export type FrameStoreItem =
  | { kind: "canonical"; message: TransportMessage | TransportQueuedMessage; context: TransportMessageRenderContext }
  | { kind: "pending"; command: UserAddMessageCommand };

/**
 * 当前活动的状态跳变。`started`：从非活动进入活动态（一轮开始）；`settled`：从活动态进入终态
 * （一轮结束）。挂起仍是活动态，不产生事件；首次构造只记基线，不产生事件。
 */
export type ActivityTransition = {
  kind: "started" | "settled";
  status: TransportRunStatus;
};

type StoreSnapshot = {
  state: TransportState;
  items: readonly FrameStoreItem[];
  isRunning: boolean;
  pendingCommands: readonly UserAddMessageCommand[];
  resyncRequired: boolean;
};

type Listener = () => void;
type ActivityListener = (transition: ActivityTransition) => void;

type ScheduledCommit = {
  cancel: () => void;
};

type PublicationKind = "stream" | "canonical" | "pending";

export type TransportFrameStoreOptions = {
  /** 注入开发期性能探针；生产构建不传入该能力。 */
  performanceProbe?: AssistantPerformanceProbe;
};

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
  if (kind === "full" && frame.state === undefined) {
    throw new Error("full transport frame must include state");
  }
  if (frame.resync_reason !== undefined && frame.resync_reason !== null && typeof frame.resync_reason !== "string") {
    throw new Error("transport frame resync_reason is invalid");
  }
  return {
    task_id: frame.task_id as number,
    kind,
    mutations,
    state: frame.state === undefined ? undefined : parseTransportState(frame.state),
    resync_reason: frame.resync_reason as string | null | undefined,
  };
}

/**
 * 为单个 task 持有前端的 Transport frame 投影。
 *
 * mutation 只复制路径上的 state 祖先；发生变化的消息得到新的源对象，未变化的历史消息
 * 保持原对象身份，以便 assistant-ui 的按消息订阅复用转换结果。它只是渲染工作副本，
 * 不建立第二个持久化事实源，也不会向 wire state 添加排序或版本字段。流式消息内容在
 * 下一动画帧统一通知订阅者，结构变化和活动状态变化则立即通知，保证 active row 不会被同一帧内
 * 的多个 token 重复提交。
 *
 * 「一轮输出结束」只由这里判定：每次 state 变化后比较 `activity.status`，活动态（运行中 / 挂起）
 * 进入终态时向活动监听者发一次 `settled`。连接断开是另一件事，由 transport hook 处理，这里不感知。
 *
 * 历史窗口：`state.messages` 只含已加载的一段，`messageOffset` 是它前面还没加载的条数。增量路径里的
 * 消息下标是完整历史里的位置，只在 `localize` 这一处换算成本地位置；指向未加载消息的增量丢弃，
 * 那一页加载时后端会给出它当时的内容。翻页请求在途时这些增量先缓存，页到达后回放落在页内的，
 * 免得页内容恰好在请求期间更新而丢失。位置只在完整快照处才会变：完整快照替换窗口、作废在途翻页。
 */
export class TransportFrameStore {
  private state: TransportState;
  private items: FrameStoreItem[] = [];
  private canonicalItems: FrameStoreItem[] = [];
  private pendingCommands: readonly UserAddMessageCommand[] = [];
  private resyncRequired = false;
  private snapshot: StoreSnapshot;
  private readonly listeners = new Set<Listener>();
  private readonly activityListeners = new Set<ActivityListener>();
  private scheduledCommit: ScheduledCommit | null = null;
  private scheduledFrameTimestamp: number | null = null;
  private readonly performanceProbe: AssistantPerformanceProbe | undefined;
  private historyGeneration = 0;
  private historyFetch: (HistoryFetch & { buffered: TransportMutation[] }) | null = null;

  public constructor(initialState: TransportState, options: TransportFrameStoreOptions = {}) {
    this.state = initialState;
    this.performanceProbe = options.performanceProbe;
    this.rebuildAllItems();
    this.snapshot = this.makeSnapshot();
  }

  public subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** 订阅当前活动的「开始 / 结束」跳变；监听者在本次 publication 之后同步调用。 */
  public subscribeActivity = (listener: ActivityListener): (() => void) => {
    this.activityListeners.add(listener);
    return () => this.activityListeners.delete(listener);
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
      this.publish();
      this.recordFrame(frame, startedAt);
      return;
    }
    if (frame.kind === "full") {
      if (!frame.state) throw new Error("full transport frame must include state");
      this.resyncRequired = false;
      this.replaceState(frame.state);
      this.recordFrame(frame, startedAt);
      return;
    }

    const mutations = frame.mutations.flatMap((mutation) => {
      const local = this.localize(mutation);
      if (local === null && this.historyFetch) this.historyFetch.buffered.push(mutation);
      return local === null ? [] : [local];
    });
    if (mutations.length === 0) {
      this.recordFrame(frame, startedAt);
      return;
    }
    const previous = this.state;
    const contentOnlyIndexes = new Set<number>();
    let structural = false;
    for (const mutation of mutations) {
      if (isMessageContentMutation(mutation, this.state)) {
        contentOnlyIndexes.add(mutation.path[1] as number);
      } else {
        structural = true;
      }
      this.state = applyMutation(this.state, mutation);
    }
    if (structural) {
      this.rebuildAllItems();
      this.reconcilePending(previous);
      this.publishNow();
      this.emitActivityTransition(previous);
    } else {
      this.refreshMessageItems(contentOnlyIndexes);
      const activeRowOnly = [...contentOnlyIndexes].every((index) => this.isActiveRowIndex(index));
      if (activeRowOnly && contentOnlyIndexes.size > 0) this.publishOnNextAnimationFrame();
      else this.publishNow();
    }
    this.recordFrame(frame, startedAt);
  }

  /** 开始往上翻一页；已加载到最早或已有在途翻页时返回 null。 */
  public beginHistoryFetch(): HistoryFetch | null {
    if (this.state.messageOffset === 0 || this.historyFetch) return null;
    this.historyFetch = { generation: this.historyGeneration, end: this.state.messageOffset, buffered: [] };
    return { generation: this.historyGeneration, end: this.state.messageOffset };
  }

  /**
   * 把取到的一页插到已加载消息前面，并回放请求期间落在这一页里的增量。窗口已被完整快照替换、
   * 或这一页与已加载部分不衔接时丢弃，返回 false。
   *
   * 不经过 `reconcilePending`：页里的用户消息是更早的历史，不是刚并入的乐观命令。
   */
  public completeHistoryFetch(fetch: HistoryFetch, page: TransportHistoryPage): boolean {
    const current = this.historyFetch;
    if (!current || current.generation !== fetch.generation || current.generation !== this.historyGeneration) return false;
    this.historyFetch = null;
    if (page.messageOffset + page.messages.length !== this.state.messageOffset) return false;
    this.cancelScheduledCommit();
    this.state = { ...this.state, messages: [...page.messages, ...this.state.messages], messageOffset: page.messageOffset };
    for (const mutation of current.buffered) {
      const local = this.localize(mutation);
      if (local !== null) this.state = applyMutation(this.state, local);
    }
    this.rebuildAllItems();
    this.publishNow();
    return true;
  }

  /** 翻页失败或被放弃：结束在途状态，之后可以重新翻。 */
  public abortHistoryFetch(fetch: HistoryFetch): void {
    if (this.historyFetch?.generation === fetch.generation && this.historyFetch.end === fetch.end) this.historyFetch = null;
  }

  public setPendingCommands(commands: readonly UserAddMessageCommand[]): void {
    this.pendingCommands = [...commands];
    this.composeItems();
    this.publish(null, "pending");
  }

  /** 去掉被拒的那条乐观命令：有 commandId 按身份匹配，否则按引用匹配。 */
  public clearPendingCommand(rejected: UserAddMessageCommand): void {
    if (this.pendingCommands.length === 0) return;
    this.pendingCommands = this.pendingCommands.filter((command) => (
      typeof command.commandId === "string"
        ? command.commandId !== rejected.commandId
        : command !== rejected
    ));
    this.composeItems();
    this.publish(null, "pending");
  }

  /** 导入一份完整 state（与 full 帧同一语义：活动跳变照常判定、乐观命令按出现的消息收敛）。 */
  public importState(state: TransportState): void {
    this.resyncRequired = false;
    this.replaceState(state);
  }

  /**
   * 立即执行已排队的流式提交。
   *
   * 生产代码通常不需要调用它；它用于连接结束前和确定性测试，确保排队的 active row
   * 更新不会在之后再次触发一次渲染通知。
   */
  public flushScheduledPublication(): void {
    if (!this.scheduledCommit) return;
    const frameTimestamp = this.scheduledFrameTimestamp;
    this.cancelScheduledCommit();
    this.publish(frameTimestamp, "stream");
  }

  private replaceState(state: TransportState): void {
    const previous = this.state;
    this.cancelScheduledCommit();
    // 完整快照重新划定窗口：已加载的旧页随之丢弃，在途翻页按代次作废。
    this.historyGeneration += 1;
    this.historyFetch = null;
    this.state = state;
    this.rebuildAllItems();
    this.reconcilePending(previous);
    this.publish();
    this.emitActivityTransition(previous);
  }

  /** 把完整历史里的消息位置换成本地位置；指向尚未加载的更早消息时返回 null。 */
  private localize(mutation: TransportMutation): TransportMutation | null {
    const [head, index, ...rest] = mutation.path;
    if (head !== "messages" || typeof index !== "number") return mutation;
    const local = index - this.state.messageOffset;
    return local < 0 ? null : { ...mutation, path: ["messages", local, ...rest] };
  }

  /**
   * 乐观命令的收敛：snapshot 里新出现了用户消息（排队或已并入）就按出现顺序去掉同样数量的最早
   * 乐观命令。命令被拒由提交方按 HTTP 结果经 `clearPendingCommand` 收回，不经这里。
   *
   * 只数完整历史里不早于此前窗口起点的消息：新窗口可能比旧窗口起得更早（编辑重跑移出消息后整体
   * 前移），那些更早的用户消息只是之前没加载，不是刚出现的。
   */
  private reconcilePending(previous: TransportState): void {
    if (this.pendingCommands.length === 0) return;
    const known = userMessageIds(previous, previous.messageOffset);
    const appeared = [...userMessageIds(this.state, previous.messageOffset)].filter((id) => !known.has(id)).length;
    if (appeared === 0) return;
    this.pendingCommands = this.pendingCommands.slice(appeared);
    this.composeItems();
  }

  /**
   * 比较前后两份 state 的当前活动。同一份 full 帧里旧 Run 已结束、新 Run 已开始（重连期间错过
   * 了中间帧）时两端都是活动态，按 `runId` 变化补一次 `settled` 再发 `started`，旧 Run 的结局取
   * 它最后一条助手消息的 `outcome`。
   */
  private emitActivityTransition(previous: TransportState): void {
    const before = previous.activity;
    const after = this.state.activity;
    const transitions: ActivityTransition[] = [];
    const wasActive = before !== null && isActiveRunStatus(before.status);
    const isActive = after !== null && isActiveRunStatus(after.status);
    if (wasActive && isActive && before.runId !== after.runId) {
      transitions.push({ kind: "settled", status: outcomeStatusOf(this.state.messages, before.runId) });
      transitions.push({ kind: "started", status: after.status });
    } else if (isActive && !wasActive) {
      transitions.push({ kind: "started", status: after.status });
    } else if (wasActive && after !== null && isTerminalRunStatus(after.status)) {
      transitions.push({ kind: "settled", status: after.status });
    }
    for (const transition of transitions) {
      if (transition.kind === "settled") this.performanceProbe?.recordTerminalConvergence();
      for (const listener of this.activityListeners) listener(transition);
    }
  }

  private rebuildAllItems(): void {
    const previousById = new Map(
      this.canonicalItems
        .filter((item): item is Extract<FrameStoreItem, { kind: "canonical" }> => item.kind === "canonical")
        .map((item) => [item.message.id, item]),
    );
    const runEndIds = lastAssistantIdsByRun(this.state.messages);
    this.canonicalItems = this.state.messages.map((message) => {
      const context = this.contextFor(message, runEndIds);
      const previous = previousById.get(message.id);
      if (previous && previous.message === message && sameContext(previous.context, context)) return previous;
      return { kind: "canonical", message, context };
    });
    this.composeItems();
  }

  /** 只有消息内容变化（流式 token、工具状态）时就地替换这几条，不重算其余消息。 */
  private refreshMessageItems(indexes: ReadonlySet<number>): void {
    if (indexes.size === 0) return;
    this.canonicalItems = [...this.canonicalItems];
    for (const index of indexes) {
      const current = this.canonicalItems[index];
      const message = this.state.messages[index];
      if (!current || current.kind !== "canonical" || !message) continue;
      this.canonicalItems[index] = { kind: "canonical", message, context: current.context };
    }
    this.composeItems();
  }

  private contextFor(message: TransportMessage, runEndIds: ReadonlySet<string>): TransportMessageRenderContext {
    const activity = this.state.activity;
    const isRunEnd = runEndIds.has(message.id);
    // 只有当前活动那一轮的回复末尾消费活动（运行中状态、实时用量）；其余消息不挂，活动元数据
    // 变化时不必重建它们。
    return {
      runId: message.runId,
      outcome: message.outcome,
      isRunEnd,
      activity: isRunEnd && activity !== null && activity.runId === message.runId ? activity : null,
    };
  }

  private composeItems(): void {
    const pending: FrameStoreItem[] = this.pendingCommands.map((command) => ({
      kind: "pending",
      command,
    }));
    const queued: FrameStoreItem[] = (this.state.queued_messages ?? []).map((message) => ({
      kind: "canonical",
      message,
      context: QUEUED_CONTEXT,
    }));
    this.items = [...this.canonicalItems, ...queued, ...pending];
  }

  private makeSnapshot(): StoreSnapshot {
    return {
      state: this.state,
      items: this.items,
      isRunning: this.pendingCommands.length > 0 || isActiveRunStatus(this.state.activity?.status),
      pendingCommands: this.pendingCommands,
      resyncRequired: this.resyncRequired,
    };
  }

  private publish(
    frameTimestamp: number | null = null,
    publicationKind: PublicationKind = "canonical",
  ): void {
    const startedAt = this.performanceProbe?.startTiming() ?? null;
    this.snapshot = this.makeSnapshot();
    if (this.performanceProbe) {
      this.performanceProbe.recordPublication({
        durationMs: this.performanceProbe.finishTiming(startedAt),
        itemCount: this.items.length,
        activeMessageId: this.state.messages.at(-1)?.id ?? null,
        publicationKind,
        animationFrameTimestamp: frameTimestamp,
        terminalConvergence: false,
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
    this.scheduledCommit = scheduleWebViewCommit((frameTimestamp) => {
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

  /** 流式内容只合并到下一动画帧提交给「当前活动的最后一条消息」，其它消息的变化立即提交。 */
  private isActiveRowIndex(index: number): boolean {
    const activity = this.state.activity;
    const message = this.state.messages[index];
    return index === this.state.messages.length - 1
      && message !== undefined
      && activity !== null
      && message.runId === activity.runId
      && isActiveRunStatus(activity.status);
  }
}

const QUEUED_CONTEXT: TransportMessageRenderContext = {
  runId: null,
  outcome: null,
  isRunEnd: false,
  activity: null,
};

/** 判断 mutation 是否只改写某条已存在消息的内部内容（不增删消息、不改结局）。路径已是本地位置。 */
function isMessageContentMutation(
  mutation: TransportFrameEnvelope["mutations"][number],
  state: TransportState,
): boolean {
  return mutation.path[0] === "messages"
    && typeof mutation.path[1] === "number"
    && mutation.path[1] < state.messages.length
    && mutation.path.length > 2
    && mutation.path[2] === "parts";
}

/** 某个 Run 的结局状态：取它最后一条带 `outcome` 的消息；投影尚未写入时按已完成处理。 */
function outcomeStatusOf(messages: readonly TransportMessage[], runId: number): TransportRunStatus {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.runId === runId && message.outcome !== null) return message.outcome.status;
  }
  return "completed";
}

/** 完整历史中位置不早于 `from` 的用户消息 id，加上排队消息。 */
function userMessageIds(state: TransportState, from: number): Set<string> {
  const ids = new Set<string>();
  state.messages.forEach((message, index) => {
    if (message.role === "user" && state.messageOffset + index >= from) ids.add(message.id);
  });
  for (const message of state.queued_messages ?? []) ids.add(message.id);
  return ids;
}

/** 每个 Run 最后一条助手消息的 id：回复末尾（用量、fork、诊断）只挂在这些消息上。 */
function lastAssistantIdsByRun(messages: readonly TransportMessage[]): Set<string> {
  const lastByRun = new Map<number, string>();
  for (const message of messages) {
    if (message.role === "assistant") lastByRun.set(message.runId, message.id);
  }
  return new Set(lastByRun.values());
}

function sameContext(left: TransportMessageRenderContext, right: TransportMessageRenderContext): boolean {
  return left.runId === right.runId
    && left.outcome === right.outcome
    && left.isRunEnd === right.isRunEnd
    && left.activity === right.activity;
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
  const message = toPendingUserMessage(item.command);
  if (!message) throw new Error("pending add-message command has no renderable parts");
  return message;
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
