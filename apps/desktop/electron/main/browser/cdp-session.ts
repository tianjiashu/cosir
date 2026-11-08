import type { WebContents } from "electron";

import type { HostMethodError } from "../host-link/methods";
import { browserError } from "./errors";

const DEFAULT_TIMEOUT_MS = 10_000;

/** 事件来自哪个会话：跨进程子框架的子会话（`page-frames.ts`），主会话为 undefined。 */
type EventHandler = (params: unknown, sessionId: string | undefined) => void;
export type SendOptions = {
  signal?: AbortSignal;
  /** 通知操作所有者：这是未经取消/超时包装的真实命令完成。 */
  onCommand?: (command: Promise<unknown>) => void;
  /** 通过取消与暂停闸门后、原生派发前登记输入来源。 */
  onDispatch?: () => void;
  timeoutMs?: number;
  browserSide?: boolean;
  /** 跨进程子框架的子会话（`page-frames.ts`）；省略即页面的主会话。 */
  sessionId?: string;
};

/**
 * 一个标签的 CDP 会话：宿主里 CDP 命令的唯一出口与事件的唯一入口。方法集合由调用方（本目录的各模块）固定，
 * 不向链路透传任意 CDP。
 *
 * 闸门：页面停着（本标签或同进程别的标签在等 JS 弹窗作答，由 `BrowserHost` 按进程判断，经 `pausedBy` 给出）时
 * 命令等不到回复——新发的直接以那个原因失败，在途的在页面停住那一刻（`interrupt`）失败。等加载之类的等待也据
 * `pausedSignal` 提前结束。命令另带时限：渲染进程卡死时命令会一直不返回。
 *
 * 跨进程子框架经自动附加得到子会话（flatten：同一个调试器连接，命令与事件带 sessionId），与主会话共用这里的闸门
 * 与时限。事件按方法名分发给订阅者（`on`），带上来源会话；没有订阅者的事件丢弃。
 *
 * 浏览器进程自己作答的命令（`browserSide`，如开关拖放拦截）不经页面，页面停着也照常回复，所以不过闸门：收尾时
 * 撤销设置靠它，否则动作中途弹窗会让设置留在页面上。
 */
export class CdpSession {
  private readonly handlers = new Map<string, Set<EventHandler>>();
  /** 页面停住时中止，让在途命令与等待立即结束；每次中止后换新的。 */
  private gate = new AbortController();

  constructor(
    private readonly contents: WebContents,
    private readonly pausedBy: () => HostMethodError | null,
    private readonly onDetach: (reason: string) => void,
    private readonly onCommand: (command: Promise<unknown>) => void = () => undefined,
  ) {}

  attach(): void {
    const { debugger: cdp } = this.contents;
    cdp.attach("1.3");
    // 主会话的事件 sessionId 是空串（实测），统一成 undefined。
    cdp.on("message", (_event, method, params, sessionId) => {
      for (const handler of this.handlers.get(method) ?? []) handler(params, sessionId || undefined);
    });
    cdp.on("detach", (_event, reason) => this.onDetach(reason));
  }

  detach(): void {
    if (this.contents.debugger.isAttached()) this.contents.debugger.detach();
  }

  /** 订阅一个 CDP 事件，至多与会话同生命周期（标签关闭时随 webContents 一起释放）；返回退订函数。 */
  on<T>(method: string, handler: (params: T, sessionId: string | undefined) => void): () => void {
    const set = this.handlers.get(method) ?? new Set<EventHandler>();
    set.add(handler as EventHandler);
    this.handlers.set(method, set);
    return () => set.delete(handler as EventHandler);
  }

  /** 页面下一次停住时中止的信号。 */
  get pausedSignal(): AbortSignal {
    return this.gate.signal;
  }

  /** 页面停住了：在途命令与等待立即以 `reason` 结束。 */
  interrupt(reason: HostMethodError): void {
    this.gate.abort(reason);
    this.gate = new AbortController();
  }

  send<T = unknown>(method: string, params: Record<string, unknown> = {}, options: SendOptions = {}): Promise<T> {
    const { signal, timeoutMs = DEFAULT_TIMEOUT_MS, browserSide = false, sessionId } = options;
    signal?.throwIfAborted();
    const paused = browserSide ? null : this.pausedBy();
    if (paused) return Promise.reject(paused);
    const gate = browserSide ? new AbortController().signal : this.gate.signal;
    options.onDispatch?.();
    const command = this.contents.debugger.sendCommand(method, params, sessionId) as Promise<T>;
    this.onCommand(command);
    options.onCommand?.(command);
    return new Promise<T>((resolve, reject) => {
      const clear = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        gate.removeEventListener("abort", onPaused);
      };
      const fail = (error: unknown) => { clear(); reject(error); };
      const onAbort = () => fail(signal!.reason);
      const onPaused = () => fail(gate.reason);
      const timer = setTimeout(() => fail(browserError("PAGE_UNRESPONSIVE", `the page did not respond to ${method}`)), timeoutMs);
      signal?.addEventListener("abort", onAbort, { once: true });
      gate.addEventListener("abort", onPaused, { once: true });
      command.then(
        (value) => { clear(); resolve(value); },
        fail,
      );
    });
  }
}
