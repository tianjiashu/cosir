import { requestHistoryPage } from "@/lib/assistant/transport/history-page-client";
import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import type { ToolCalls } from "@/lib/assistant/tools/tool-call-index";
import type { TransportState } from "@/lib/assistant/contract";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { markTaskRequestsUnsynced, reconcileCommands, releaseCommandObservation } from "@/lib/assistant/command-request-store";
import { parseTransportFrame, TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { SubscriptionRecovery, type TransportIssue } from "@/lib/assistant/transport/subscription-recovery";
import { transportMessageCount } from "@/lib/assistant/transport/transport-state-operations";
import { classifyBackendTransition } from "@/lib/backend/backend-transition";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime, type BackendRuntimeSnapshot } from "@/lib/backend/runtime-config";
import { requestOk } from "@/lib/http/client";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TraceId } from "@/lib/logging/trace-id";

/**
 * 连接的生命周期：首个完整帧到达前是 `connecting`（界面显示骨架），到达后 `ready` 并持有 store；
 * attach 回 404 即 `missing`（Task 已不存在，不再重连）。连接问题（断流、重连耗尽）另以 `issue` 呈现，
 * 与生命周期正交：首帧前与首帧后的提示同一来源。
 */
type TransportConnectionStatus =
  | { kind: "connecting" }
  | { kind: "ready"; store: TransportFrameStore }
  | { kind: "missing" };

export type TransportConnectionSnapshot = {
  status: TransportConnectionStatus;
  issue: TransportIssue | null;
};

export type TaskTransportConnectionOptions = {
  taskId: number;
  /** 长订阅与命令共用的链路 id（经 `X-Trace-Id` 传给后端）。 */
  traceId: TraceId;
  /** 日志里区分主对话与 Workbench 阅读页。 */
  surface: "conversation" | "workbench";
  performanceProbe?: AssistantPerformanceProbe | null;
  /** canonical state 每次提交时调用（含首帧建 store 那一次）：主对话据此同步页面级镜像 store。 */
  onStateCommit?: (state: TransportState, toolCalls: ToolCalls) => void;
};

/** 一次提交的发送与失败处理，由 runtime 在提交时给出（它们依赖 runtime 的配置与草稿）。 */
export type TransportSubmission = {
  send: (command: UserAddMessageCommand) => Promise<void>;
  onError: (error: Error, commands: readonly UserAddMessageCommand[]) => Promise<void> | void;
};

type StreamHandle = { controller: AbortController };

/**
 * 一个 Task 的 Transport 连接：Task 长订阅、投影 store、命令提交、有限重连与后端实例变化，全部在这里，
 * 不依赖 React。主对话与 Workbench 阅读页共用；外壳（React）只按快照渲染、在挂载期间 start / stop。
 *
 * - **首帧即初始状态**：不另读快照。attach 首帧总是完整快照（后端在锁内原子地挂订阅、取快照），store
 *   在它到达时才创建，之后的帧交给 store；首帧不是 full 是协议错误，按断开处理。重连、背压重同步与后端
 *   恢复同样只是重开订阅，所以首次打开就是第一次连接，不是另一条路径。
 * - **连接所有权**：句柄的结束只有 `end` 一处（中止请求，底层连接随之关闭；abort 幂等）。任何结束路径
 *   都经它，不再是当前的句柄一定已被中止——只丢引用不中止，健康的连接会一直挂着，几次重连就占满
 *   浏览器对本机后端的 6 条连接。
 * - **断开怎么办**：404 说明 Task 已不存在（打开时或订阅期间被删除），进入 `missing`、不再重连；其余
 *   断开交给 `SubscriptionRecovery` 有限重连（背压重同步立即重开、不计预算）。
 * - **后端实例**：自行订阅后端快照。不可用时关掉订阅、停掉重连计时；恢复或换代时重开（首帧即完整状态），
 *   并提示正在同步。
 * - **命令**：只投递，结果经订阅到达。发送不依赖订阅：连接已停止（切走 Task）时迟到的上传后发送照常投递、
 *   失败照常回调，否则用户的消息会无声丢失；提交成功时订阅若断着（重连间隙）就立即补上。
 */
export class TaskTransportConnection {
  private active: StreamHandle | null = null;
  private enabled = false;
  private store: TransportFrameStore | null = null;
  private snapshot: TransportConnectionSnapshot = { status: { kind: "connecting" }, issue: null };
  private readonly listeners = new Set<() => void>();
  private readonly recovery: SubscriptionRecovery;
  private backend: BackendRuntimeSnapshot = getBackendRuntimeSnapshot();
  private unsubscribeBackend: (() => void) | null = null;
  private releaseStore: (() => void) | null = null;

  public constructor(private readonly options: TaskTransportConnectionOptions) {
    this.recovery = new SubscriptionRecovery({
      taskId: options.taskId,
      traceId: options.traceId,
      attach: () => void this.open(),
      backend: getBackendRuntimeSnapshot,
      setIssue: (issue) => this.update({ issue }),
    });
  }

  public subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  public getSnapshot = (): TransportConnectionSnapshot => this.snapshot;

  public get isOpen(): boolean {
    return this.active !== null;
  }

  /** 外壳挂载时调用：开始跟随后端实例并打开订阅。可在 `stop` 之后再次调用（StrictMode 的重放）。 */
  public start = (): void => {
    if (this.enabled) return;
    this.enabled = true;
    this.recovery.setActive(true);
    this.backend = getBackendRuntimeSnapshot();
    this.unsubscribeBackend = subscribeBackendRuntime(this.onBackendChange);
    if (this.store) this.observeStore(this.store);
    void this.open();
  };

  /** 外壳卸载时调用：关闭订阅、停掉重连与后端跟随。store 留着，迟到的提交仍可收尾。 */
  public stop = (): void => {
    if (!this.enabled) return;
    this.enabled = false;
    this.recovery.setActive(false);
    this.recovery.resetBudget();
    this.unsubscribeBackend?.();
    this.unsubscribeBackend = null;
    this.releaseStore?.();
    this.releaseStore = null;
    releaseCommandObservation(this.options.taskId);
    if (this.active) this.end(this.active);
  };

  /** 用户「重新同步」：清零重连预算并立即重开订阅（首帧即完整状态）。 */
  public resync = (): void => {
    if (this.snapshot.status.kind === "missing") return;
    this.recovery.resync(null);
  };

  /**
   * 往上翻一页更早的历史并插到已加载消息前面；已加载到最早、已有在途翻页或尚无 store 时什么也不做。
   *
   * 失败只记日志、结束在途状态：用户继续往上滚会再触发。位置失效（409）说明窗口已被完整快照
   * 替换，store 也会按代次丢弃这一页。
   */
  public loadEarlierHistory = async (): Promise<void> => {
    const store = this.store;
    const fetch = store?.beginHistoryFetch();
    if (!store || !fetch) return;
    const loadedIds = new Set(store.getSnapshot().state.messages.map((message) => message.id));
    try {
      const page = await requestHistoryPage(this.options.taskId, fetch.end, loadedIds, { traceId: this.options.traceId });
      if (!store.completeHistoryFetch(fetch, page)) {
        void frontendLog("INFO", "assistant_history_page_discarded", "更早历史的一页已过期，丢弃", {
          data: { taskId: this.options.taskId, end: fetch.end },
        });
      }
    } catch (error) {
      store.abortHistoryFetch(fetch);
      void frontendLog("WARNING", "assistant_history_page_failed", "加载更早的对话历史失败", {
        traceId: this.options.traceId,
        data: { taskId: this.options.taskId, end: fetch.end },
        error,
      });
    }
  };

  /** 提交一条用户消息。只有 `ready` 之后的 runtime 会调用（发送入口随 runtime 挂载）。 */
  public async submit(command: UserAddMessageCommand, submission: TransportSubmission): Promise<void> {
    const store = this.requireStore();
    store.setPendingCommands([...store.getSnapshot().pendingCommands, command]);
    try {
      await submission.send(command);
      this.update({ issue: null });
      if (this.enabled && !this.active) void this.open();
    } catch (error) {
      // 请求未发出（如附件已失效）与后端拒绝同属提交失败，交给同一个回调恢复草稿并提示。
      store.clearPendingCommand(command);
      await submission.onError(error instanceof Error ? error : new Error(String(error)), [command]);
    }
  }

  /** 打开（或替换）Task 长订阅：先收到 full 帧，之后持续接收增量；不携带请求体、不触发业务执行。 */
  public open = async (): Promise<void> => {
    if (!this.enabled || !this.backend.available || this.snapshot.status.kind === "missing") return;
    if (this.active) this.end(this.active);
    const handle: StreamHandle = { controller: new AbortController() };
    this.active = handle;
    let failure: Error | null = null;
    try {
      // 非 2xx 由 requestOk 解析为结构化 HttpError（响应体原文不进入错误消息）。
      const response = await requestOk(`/tasks/${this.options.taskId}/assistant/attach`, {
        method: "POST",
        headers: { Accept: "text/event-stream" },
        signal: handle.controller.signal,
        traceId: this.options.traceId,
      });
      if (this.active !== handle) return;
      this.onConnected(response);
      await this.readSse(response, handle);
    } catch (error) {
      failure = error instanceof Error ? error : new Error(String(error));
    }
    // 被 stop 或新的 open 替换时，句柄已由替换方结束，这里不处理。
    const replaced = this.active !== handle;
    this.end(handle);
    if (replaced) return;
    this.onDisconnected(failure, failure === null && (this.store?.getSnapshot().resyncRequired ?? false));
  };

  private end(handle: StreamHandle): void {
    handle.controller.abort();
    if (this.active === handle) this.active = null;
  }

  private onConnected(response: Response): void {
    void frontendLog("INFO", "assistant_transport_subscription_opened", "Task 长订阅已建立", {
      traceId: this.options.traceId,
      data: {
        taskId: this.options.taskId,
        surface: this.options.surface,
        status: response.status,
        contentType: response.headers.get("content-type"),
        taskHeader: response.headers.get("x-cosir-task-id"),
        traceHeader: response.headers.get("x-trace-id"),
      },
    });
    this.recovery.markConnected();
    this.update({ issue: null });
  }

  private onDisconnected(error: Error | null, resync: boolean): void {
    if (error instanceof HttpError && error.status === 404) {
      void frontendLog("WARNING", "assistant_transport_task_missing", "Task 已不存在，停止订阅", {
        traceId: this.options.traceId,
        data: { taskId: this.options.taskId, surface: this.options.surface },
      });
      this.recovery.resetBudget();
      this.update({ status: { kind: "missing" }, issue: null });
      return;
    }
    void frontendLog(error ? "WARNING" : "INFO", "assistant_transport_subscription_closed", "Task 长订阅意外结束", {
      traceId: this.options.traceId,
      data: {
        taskId: this.options.taskId,
        surface: this.options.surface,
        resync,
        activityStatus: this.store?.getSnapshot().state.activity?.status ?? null,
      },
      error: error ?? undefined,
    });
    // 断开期间可能错过「停下、又开出新运行」：重连拿到的完整快照分不出来，停止结果改为待确认。
    markTaskRequestsUnsynced(this.options.taskId);
    this.recovery.reconnect(resync ? "resync_required" : "disconnected");
  }

  private readonly onBackendChange = (): void => {
    const previous = this.backend;
    const next = getBackendRuntimeSnapshot();
    this.backend = next;
    const transition = classifyBackendTransition(previous, next);
    if (!transition) return;
    if (transition.kind === "unavailable") {
      void frontendLog("INFO", "assistant_backend_unavailable", "Task 连接检测到本机后端不可用", {
        traceId: this.options.traceId,
        data: { taskId: this.options.taskId, generation: previous.generation },
      });
      this.recovery.resetBudget();
      if (this.active) this.end(this.active);
      return;
    }
    void frontendLog("INFO", "assistant_backend_restored", "Task 连接检测到本机后端恢复或更换实例", {
      traceId: this.options.traceId,
      data: {
        taskId: this.options.taskId,
        previousGeneration: transition.previousGeneration,
        generation: transition.generation,
        wasUnavailable: transition.wasUnavailable,
      },
    });
    // 此前的进程内订阅已失效（后端把遗留 active Run 收敛为 cancelled）：重开订阅，首帧原地对齐。
    this.recovery.restored(this.store ? { message: "本机后端已恢复，正在同步当前对话…", canResync: true } : null);
    void this.open();
  };

  private applyFrame(frame: ReturnType<typeof parseTransportFrame>): void {
    if (this.store) {
      this.store.applyFrame(frame);
      return;
    }
    if (frame.kind !== "full") throw new Error("Task 长订阅的首帧必须是完整快照");
    const store = new TransportFrameStore(frame.state, { performanceProbe: this.options.performanceProbe ?? undefined });
    this.store = store;
    void frontendLog("INFO", "assistant_transport_initial_state", "Task 长订阅首帧已到达", {
      traceId: this.options.traceId,
      data: {
        taskId: this.options.taskId,
        surface: this.options.surface,
        messageCount: transportMessageCount(frame.state),
        activityTurn: frame.state.activity?.turn ?? null,
        activityStatus: frame.state.activity?.status ?? null,
      },
    });
    if (this.enabled) this.observeStore(store);
    this.update({ status: { kind: "ready", store } });
  }

  /** canonical state 的派生只挂在这里：命令对账、页面镜像同步与活动跳变（性能探针、结束日志）。 */
  private observeStore(store: TransportFrameStore): void {
    this.releaseStore?.();
    let committed: TransportState | undefined;
    const commit = () => {
      const current = store.getSnapshot();
      if (committed === current.state) return;
      committed = current.state;
      reconcileCommands(this.options.taskId, current.state.activity);
      this.options.onStateCommit?.(current.state, current.toolCalls);
    };
    const unsubscribe = store.subscribe(commit);
    const unsubscribeActivity = store.subscribeActivity((transition) => {
      const probe = this.options.performanceProbe;
      if (transition.kind === "started") {
        probe?.beginStream();
        return;
      }
      probe?.finishStream({ activityTurn: store.getSnapshot().state.activity?.turn ?? null, activityStatus: transition.status });
      void frontendLog("INFO", "assistant_activity_settled", "Task 当前活动已结束", {
        traceId: this.options.traceId,
        data: {
          taskId: this.options.taskId,
          activityTurn: store.getSnapshot().state.activity?.turn ?? null,
          status: transition.status,
          messageCount: transportMessageCount(store.getSnapshot().state),
        },
      });
    });
    commit();
    this.releaseStore = () => {
      unsubscribe();
      unsubscribeActivity();
    };
  }

  private requireStore(): TransportFrameStore {
    if (!this.store) throw new Error("Task 连接尚未收到首帧，不能提交命令");
    return this.store;
  }

  private update(change: Partial<TransportConnectionSnapshot>): void {
    const next = { ...this.snapshot, ...change };
    if (next.status === this.snapshot.status && next.issue === this.snapshot.issue) return;
    this.snapshot = next;
    for (const listener of [...this.listeners]) listener();
  }

  private async readSse(response: Response, handle: StreamHandle): Promise<void> {
    if (!response.body) throw new Error("Assistant frame SSE response has no body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const applyRecord = (record: string) => {
      const data = record.split("\n").find((line) => line.startsWith("data:"))?.slice("data:".length).trim();
      if (!data || data === "[DONE]" || this.active !== handle) return;
      this.applyFrame(parseTransportFrame(JSON.parse(data)));
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const records = buffer.split("\n\n");
      buffer = records.pop() ?? "";
      records.forEach(applyRecord);
    }
    buffer += decoder.decode();
    if (buffer.trim()) applyRecord(buffer);
    // SSE EOF 可能早于渲染进程的下一次动画帧；先提交最后一批 active row 内容。
    if (this.active === handle) this.store?.flushScheduledPublication();
  }
}
