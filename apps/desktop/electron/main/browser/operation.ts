import type { BrowserTab } from "./browser-tab";
import type { SendOptions } from "./cdp-session";
import { browserError } from "./errors";
import { readGeometry } from "./page-geometry";
import type { PageNode } from "./ref-registry";
import type { Box } from "./input/element";
import { logDesktopEvent } from "../desktop-log";

export type FileChooser = { node: PageNode; multiple: boolean };
export type OperationState = "running" | "stopping" | "unknown";
type Release = { method: string; params: Record<string, unknown>; document: number; confirmed: boolean };
export const OPERATION_DRAIN_MS = 1_000;

/**
 * 标签的一次修改操作。请求停止等待与 Chromium 命令完成是两件事：pending 只追踪原始命令，
 * 标签直到命令和输入释放都收敛才交出槽位。无法确认的输入保留 unknown，关闭标签可终止。
 * 不排队、不重放、不持久化。输入模块直接使用本类，所有普通命令在同一出口检查取消。
 */
export class BrowserOperation {
  readonly controller = new AbortController();
  readonly signal: AbortSignal;
  state: OperationState = "running";
  private readonly pending = new Set<Promise<unknown>>();
  private held: Release | null = null;
  private unsafe = false;
  private effectDepth = 0;
  effectStarted = false;
  private done: Promise<void> | null = null;

  constructor(readonly tab: BrowserTab, signal: AbortSignal) {
    this.signal = AbortSignal.any([signal, this.controller.signal]);
    this.signal.addEventListener("abort", () => this.change("stopping"), { once: true });
    this.record("browser_operation_started");
  }

  /** 每步结算独立；定位失败不能被前一步的副作用污染。 */
  beginStep(): void { this.effectStarted = false; }

  cdp = <T = unknown>(method: string, params: Record<string, unknown> = {}, options: SendOptions = {}): Promise<T> => {
    this.signal.throwIfAborted();
    return this.send<T>(method, params, { ...options, signal: this.signal });
  };

  private send<T>(method: string, params: Record<string, unknown>, options: SendOptions, issued?: (raw: Promise<unknown>) => void): Promise<T> {
    // Electron 可在 sendCommand 返回前同步派发 before-input-event，来源窗口必须先登记。
    return this.tab.session.send<T>(method, params, { ...options, onDispatch: () => {
      if (this.effectDepth || method === "Input.dispatchKeyEvent" || method === "Input.dispatchMouseEvent") this.tab.noteAgentInput(method);
    }, onCommand: (raw) => {
      if (this.effectDepth) this.effectStarted = true;
      this.track(raw);
      issued?.(raw);
    } });
  }

  /** 外层 API 和组件后台同步同样登记真实完成，不能只追踪输入模块直接发出的命令。 */
  track(raw: Promise<unknown>): void {
    this.pending.add(raw);
    raw.then(() => this.pending.delete(raw), () => this.pending.delete(raw));
  }

  /** 副作用必须在取消检查之后才标记为可能生效。 */
  async effect<T>(run: () => Promise<T>): Promise<T> {
    this.signal.throwIfAborted();
    this.effectDepth++;
    try { return await run(); } finally { this.effectDepth--; }
  }

  /** 按下、可选移动、释放是一单元；取消后只允许已确认且同文档的释放，不再派发后续业务输入。 */
  async inputUnit(method: string, down: Record<string, unknown>, up: Record<string, unknown>, body?: () => Promise<void>): Promise<void> {
    this.signal.throwIfAborted();
    const release: Release = { method, params: up, document: this.tab.documentGeneration, confirmed: false };
    await this.effect(async () => {
      let completed = false;
      try {
        await this.send(method, down, { signal: this.signal }, (raw) => {
          this.held = release;
          raw.then(() => { release.confirmed = true; }, () => { this.unsafe = true; });
        });
        if (body) await body();
        completed = true;
      } finally {
        if (release.confirmed && (completed || this.pending.size === 0)) await this.release();
      }
    });
    this.signal.throwIfAborted();
  }

  /** 拖放拦截等浏览器进程设置的撤销不受业务取消阻断，仍追踪真实完成。 */
  cleanup(method: string, params: Record<string, unknown>): Promise<unknown> {
    return this.send(method, params, { browserSide: true });
  }

  private async release(): Promise<void> {
    const held = this.held;
    if (!held) return;
    if (!held.confirmed || held.document !== this.tab.documentGeneration || this.tab.contents.isDestroyed()) {
      this.unsafe = true;
      throw browserError("OPERATION_UNKNOWN", "cannot safely release input after the document changed");
    }
    let dispatched = false;
    try {
      await this.send(held.method, held.params, {}, (raw) => {
        dispatched = true;
        raw.then(() => { if (this.held === held) this.held = null; }, () => { this.unsafe = true; });
      });
    } catch (error) {
      // 弹窗/时限可先终止等待；真正的 release 仍由原始 Promise 确认，不能重复派发。
      // 暂停闸门在派发前拒绝时，答完弹窗仍可释放；已派发的失败不得重放。
      if (dispatched && this.pending.size === 0) this.unsafe = true;
      throw error;
    }
  }

  /** 有界等待只限制答复，不释放仍有命令的槽位；迟到命令完成后自动尝试安全收尾。 */
  async finish(): Promise<void> {
    if (!this.done) {
      const draining = this.drain();
      this.done = draining;
      void draining.finally(() => { if (this.done === draining) this.done = null; });
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    await Promise.race([this.done, new Promise<void>((resolve) => { timer = setTimeout(resolve, OPERATION_DRAIN_MS); })]);
    clearTimeout(timer);
    if (this.tab.operation === this) this.change("unknown");
  }

  private async drain(): Promise<void> {
    this.change("stopping");
    while (this.pending.size) await Promise.allSettled([...this.pending]);
    if (this.held) {
      try { await this.release(); }
      catch (error) { logDesktopEvent("INFO", "browser", "browser_input_release_incomplete", { tab_id: this.tab.tabId }, error); }
    }
    while (this.pending.size) await Promise.allSettled([...this.pending]);
    if (this.unsafe || this.held) { this.change("unknown"); return; }
    this.record("browser_operation_settled");
    this.tab.operationSettled(this);
  }

  /** 弹窗答复这个实际事件触发一次收尾，不轮询或重放业务命令。 */
  resumeCleanup(): void {
    if (this.tab.operation !== this || this.unsafe || !this.held) return;
    if (this.done) { void this.done.then(() => this.resumeCleanup()); return; }
    if (this.state === "unknown") void this.finish();
  }

  stop(): void { this.controller.abort(browserError("CANCELLED", "the user stopped this operation")); }
  private change(state: OperationState): void {
    if (this.state === state) return;
    this.state = state;
    this.record("browser_operation_state_changed");
    this.tab.operationChanged();
  }
  private record(event: string): void {
    logDesktopEvent("INFO", "browser", event, { task_id: this.tab.taskId, tab_id: this.tab.tabId, state: this.state });
  }

  on<T>(method: string, handler: (params: T) => void): () => void { return this.tab.session.on(method, handler); }
  resolveRef(ref: string): PageNode { return this.tab.resolveRef(ref); }
  frameOffset(sessionId: string | undefined) { return this.tab.frames.offset(sessionId, this.cdp); }
  settle(): Promise<void> { return this.tab.settle(this.signal); }
  geometry() { return readGeometry(this.cdp); }
  toInput(point: { x: number; y: number }) { return this.tab.emulation.toInput(point); }
  paused() { return this.tab.paused(); }
  dialog() { return this.tab.dialog; }
  answerDialog(id: number, answer: Parameters<BrowserTab["answerDialog"]>[1]): void { this.tab.answerDialog(id, answer, "agent"); }
  fileChooser() { return this.tab.fileChooser; }
  highlight(locate: () => Promise<Box>): Promise<void> { return this.tab.highlighter.show(locate); }
}
