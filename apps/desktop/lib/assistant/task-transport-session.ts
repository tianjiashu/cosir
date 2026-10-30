import { requestHistoryPage } from "@/lib/assistant/assistant-snapshot-client";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { parseTransportFrame, type TransportFrameStore } from "@/lib/assistant/transport-frame-store";
import { requestOk } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TraceId } from "@/lib/trace";

/** 长订阅结束的原因：`error` 为 null 表示连接被后端正常关闭（EOF）；`resync` 为真表示背压重同步。 */
export type TransportDisconnect = {
  error: Error | null;
  resync: boolean;
};

export type TaskTransportSessionCallbacks = {
  /** 长订阅请求的链路 id（经 `X-Trace-Id` 传给后端），与本会话的命令共用。 */
  traceId: TraceId;
  /** 提交一条用户消息；乐观消息保留到 snapshot 里出现对应消息（或命令被拒）才清除。 */
  submitMessage?: (command: UserAddMessageCommand) => Promise<void>;
  /** 长订阅建立（attach 2xx）。 */
  onResponse?: (response: Response) => void;
  /** 长订阅意外结束（EOF、网络错误、attach 非 2xx）；主动 `close` / 被新的 `open` 替换不回调。 */
  onDisconnect?: (disconnect: TransportDisconnect) => void;
  /** 命令提交被拒或未能发出；乐观消息已先行收回。 */
  onSubmitError?: (error: Error, commands: readonly UserAddMessageCommand[]) => Promise<void> | void;
};

type StreamHandle = { controller: AbortController };

/**
 * 单个 Task 的 Transport 会话核心：一条 Task 长订阅 + 命令提交，不依赖 React。
 *
 * 订阅跟随 Task：没有 Run 时也保持连接，Run 结束不断开，只在 `close`、被新的 `open` 替换、
 * 或意外结束（交给宿主重连）时停止。旧连接用被替换的 AbortController 与句柄身份隔离，不引入
 * 协议版本字段。命令只投递：结果一律经订阅到达，提交成功后订阅若断着（重连间隙）就立即补上。
 * frame store 是唯一的投影持有者，这里只把帧交给它。
 */
export class TaskTransportSession {
  private active: StreamHandle | null = null;

  /** 请求路径相对后端：基地址在每次请求时取当前后端实例，后端换端口后重开即生效。 */
  public constructor(
    private readonly store: TransportFrameStore,
    private readonly taskId: number,
    private readonly callbacks: () => TaskTransportSessionCallbacks,
  ) {}

  /**
   * 往上翻一页更早的历史并插到已加载消息前面；已加载到最早或已有在途翻页时什么也不做。
   *
   * 失败只记日志、结束在途状态：用户继续往上滚会再触发。位置失效（409）说明窗口已被完整快照
   * 替换，store 也会按代次丢弃这一页。
   */
  public loadEarlierHistory = async (): Promise<void> => {
    const fetch = this.store.beginHistoryFetch();
    if (!fetch) return;
    const loadedIds = new Set(this.store.getSnapshot().state.messages.map((message) => message.id));
    try {
      const page = await requestHistoryPage(this.taskId, fetch.end, loadedIds, { traceId: this.callbacks().traceId });
      if (!this.store.completeHistoryFetch(fetch, page)) {
        void frontendLog("INFO", "assistant_history_page_discarded", "更早历史的一页已过期，丢弃", {
          data: { taskId: this.taskId, end: fetch.end },
        });
      }
    } catch (error) {
      this.store.abortHistoryFetch(fetch);
      void frontendLog("WARNING", "assistant_history_page_failed", "加载更早的对话历史失败", {
        traceId: this.callbacks().traceId,
        data: { taskId: this.taskId, end: fetch.end },
        error,
      });
    }
  };

  public get isOpen(): boolean {
    return this.active !== null;
  }

  public close(): void {
    this.active?.controller.abort();
    this.active = null;
  }

  /** 打开（或替换）Task 长订阅：先收到 full 帧，之后持续接收增量；不携带请求体、不触发业务执行。 */
  public open = async (): Promise<void> => {
    this.close();
    const handle: StreamHandle = { controller: new AbortController() };
    this.active = handle;
    try {
      // 非 2xx 由 requestOk 解析为结构化 HttpError（响应体原文不进入错误消息，界面经
      // userErrorMessage 只展示后端给出的可读原因）。
      const response = await requestOk(`/tasks/${this.taskId}/assistant/attach`, {
        method: "POST",
        headers: { Accept: "text/event-stream" },
        signal: handle.controller.signal,
        traceId: this.callbacks().traceId,
      });
      if (this.active !== handle) return;
      this.callbacks().onResponse?.(response);
      await this.readSse(response, handle);
      if (this.active !== handle) return;
      this.active = null;
      this.callbacks().onDisconnect?.({ error: null, resync: this.store.getSnapshot().resyncRequired });
    } catch (error) {
      if (handle.controller.signal.aborted || this.active !== handle) return;
      this.active = null;
      this.callbacks().onDisconnect?.({ error: error instanceof Error ? error : new Error(String(error)), resync: false });
    }
  };

  public async submit(command: UserAddMessageCommand): Promise<void> {
    const submit = this.callbacks().submitMessage;
    if (!submit) throw new Error("只读 Transport runtime 不接受消息提交");
    this.store.setPendingCommands([...this.store.getSnapshot().pendingCommands, command]);
    try {
      await submit(command);
      if (!this.active) void this.open();
    } catch (error) {
      // 请求未发出（如附件已失效）与后端拒绝同属提交失败，交给同一个回调恢复草稿并提示。
      this.store.clearPendingCommand(command);
      await this.callbacks().onSubmitError?.(error instanceof Error ? error : new Error(String(error)), [command]);
    }
  }

  private async readSse(response: Response, handle: StreamHandle): Promise<void> {
    if (!response.body) throw new Error("Assistant frame SSE response has no body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const applyRecord = (record: string) => {
      const data = record.split("\n").find((line) => line.startsWith("data:"))?.slice("data:".length).trim();
      if (!data || data === "[DONE]" || this.active !== handle) return;
      this.store.applyFrame(parseTransportFrame(JSON.parse(data)));
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
    // SSE EOF 可能早于宿主 WebView 的下一次动画帧；先提交最后一批 active row 内容。
    if (this.active === handle) this.store.flushScheduledPublication();
  }
}
