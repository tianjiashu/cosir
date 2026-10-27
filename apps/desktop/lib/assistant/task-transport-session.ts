import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import { parseTransportFrame, type TransportFrameStore } from "@/lib/assistant/transport-frame-store";
import { parseHttpError } from "@/lib/http/errors";

/** 长订阅结束的原因：`error` 为 null 表示连接被后端正常关闭（EOF）；`resync` 为真表示背压重同步。 */
export type TransportDisconnect = {
  error: Error | null;
  resync: boolean;
};

export type TaskTransportSessionCallbacks = {
  headers: () => Promise<Record<string, string>> | Record<string, string>;
  /**
   * 提交一条用户消息。返回 `awaitSnapshot`：为真时乐观消息保留到 snapshot 里出现对应消息（或
   * 命令被拒）才清除；编辑排队消息原地更新、不产生新消息，返回 false 即清。
   */
  submitMessage?: (command: UserAddMessageCommand) => Promise<{ awaitSnapshot: boolean }>;
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

  public constructor(
    private readonly store: TransportFrameStore,
    private readonly attachUrl: string,
    private readonly callbacks: () => TaskTransportSessionCallbacks,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

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
      const response = await this.fetchImpl(this.attachUrl, {
        cache: "no-store",
        method: "POST",
        headers: await this.callbacks().headers(),
        signal: handle.controller.signal,
      });
      // 结构化错误（code / message / retryable）由 parseHttpError 解析为 HttpError；
      // 响应体原文不进入错误消息，界面经 userErrorMessage 只展示后端给出的可读原因。
      if (!response.ok) throw await parseHttpError(response);
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
      const { awaitSnapshot } = await submit(command);
      if (!awaitSnapshot) this.store.clearPendingCommands([command]);
      if (!this.active) void this.open();
    } catch (error) {
      // 请求未发出（如附件已失效）与后端拒绝同属提交失败，交给同一个回调恢复草稿并提示。
      this.store.clearPendingCommands([command]);
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
