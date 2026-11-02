import type { WebContents } from "electron";

import type { HostMethodError } from "../host-link/methods";
import { browserError } from "./errors";

const DEFAULT_TIMEOUT_MS = 10_000;

type EventHandler = (params: unknown) => void;

/**
 * 一个标签的 CDP 会话：宿主里 CDP 命令的唯一出口与事件的唯一入口。方法集合由调用方（本目录的各模块）固定，
 * 不向链路透传任意 CDP。
 *
 * 闸门：页面停着（本标签或同进程别的标签在等 JS 弹窗作答，由 `BrowserHost` 按进程判断，经 `pausedBy` 给出）时
 * 命令等不到回复——新发的直接以那个原因失败，在途的在页面停住那一刻（`interrupt`）失败。等加载之类的等待也据
 * `pausedSignal` 提前结束。命令另带时限：渲染进程卡死时命令会一直不返回。
 *
 * 事件按方法名分发给订阅者（`on`）；没有订阅者的事件丢弃。
 */
export class CdpSession {
  private readonly handlers = new Map<string, Set<EventHandler>>();
  /** 页面停住时中止，让在途命令与等待立即结束；每次中止后换新的。 */
  private gate = new AbortController();

  constructor(
    private readonly contents: WebContents,
    private readonly pausedBy: () => HostMethodError | null,
    private readonly onDetach: (reason: string) => void,
  ) {}

  attach(): void {
    const { debugger: cdp } = this.contents;
    cdp.attach("1.3");
    cdp.on("message", (_event, method, params) => {
      for (const handler of this.handlers.get(method) ?? []) handler(params);
    });
    cdp.on("detach", (_event, reason) => this.onDetach(reason));
  }

  detach(): void {
    if (this.contents.debugger.isAttached()) this.contents.debugger.detach();
  }

  /** 订阅一个 CDP 事件，与会话同生命周期（标签关闭时随 webContents 一起释放）。 */
  on<T>(method: string, handler: (params: T) => void): void {
    const set = this.handlers.get(method) ?? new Set<EventHandler>();
    set.add(handler as EventHandler);
    this.handlers.set(method, set);
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

  send<T = unknown>(method: string, params: Record<string, unknown> = {}, options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<T> {
    const { signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
    signal?.throwIfAborted();
    const paused = this.pausedBy();
    if (paused) return Promise.reject(paused);
    const gate = this.gate.signal;
    const command = this.contents.debugger.sendCommand(method, params) as Promise<T>;
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
