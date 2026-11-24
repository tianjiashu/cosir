import type { Session, WebRequestFilter } from "electron";

import { isLoopbackHost } from "../shared/local-network-host";
import { logDesktopEvent } from "./desktop-log";

export const CAPABILITY_HEADER = "X-Cosir-Capability";
export type BackendIdentity = { baseUrl: string; token: string; generation: number };
type RequestSource = { id: number; url: string; webContentsId?: number; frame?: { url: string; parent: unknown } | null; initiatorOrigin?: string };

/**
 * 应用界面所在会话的网络请求策略。Electron 每个会话只能挂一个 `onBeforeRequest`，所以两条规则都放在这里：
 *
 * - 出站：这个会话里的页面（主窗口、开发者工具）只能访问本机回环地址（后端、开发服务）。界面会渲染模型写的内容（链接、Markdown 图片），只要它能连公网，
 *   一显示就会把模型挑的主机名、路径交出去，站点还能看到本机 IP。界面本身没有正当的公网请求，所以一律拒绝、记日志，
 *   以后新加的远程资源也会在这里被拦下，而不是各界面自己设防。重定向会再经过这里，跳不出去。
 * - 后端身份：只有主窗口顶层 frame 的直连后端请求带 capability，重定向不能提权。
 *
 * 主进程自己的请求没有 `webContentsId`，不受出站规则限制。
 */
export class RendererRequestPolicy {
  private readonly requests = new Map<number, number | null>();

  constructor(
    private readonly identity: () => BackendIdentity | null,
    private readonly windowId: () => number | undefined,
    private readonly appOrigin: string,
  ) {}

  private target(url: string, identity: BackendIdentity): boolean {
    const value = new URL(url);
    const base = new URL(identity.baseUrl);
    return ["http:", "ws:"].includes(value.protocol) && value.hostname === base.hostname && value.port === base.port;
  }

  private trusted(source: RequestSource): boolean {
    if (source.webContentsId !== this.windowId() || !source.frame || source.frame.parent !== null) return false;
    const frame = new URL(source.frame.url);
    const origin = frame.protocol === "app:" ? `${frame.protocol}//${frame.host}` : frame.origin;
    return origin === this.appOrigin && source.initiatorOrigin === this.appOrigin;
  }

  private outboundAllowed(source: RequestSource): boolean {
    if (source.webContentsId === undefined) return true;
    const url = new URL(source.url);
    if (isLoopbackHost(url.hostname)) return true;
    logDesktopEvent("WARNING", "renderer_request_policy", "renderer_outbound_blocked", { origin: url.origin });
    return false;
  }

  begin(source: RequestSource): boolean {
    if (!this.outboundAllowed(source)) return false;
    const identity = this.identity();
    const target = identity !== null && this.target(source.url, identity);
    if (!this.requests.has(source.id)) {
      this.requests.set(source.id, target && this.trusted(source) ? identity.generation : null);
    } else if (!target) {
      this.requests.set(source.id, null);
    }
    return !target || (this.trusted(source) && this.requests.get(source.id) === identity.generation);
  }

  headers(source: RequestSource, supplied: Record<string, string>): Record<string, string> {
    const headers = Object.fromEntries(Object.entries(supplied).filter(([key]) => key.toLowerCase() !== CAPABILITY_HEADER.toLowerCase()));
    const identity = this.identity();
    if (identity && this.target(source.url, identity) && this.trusted(source) && this.requests.get(source.id) === identity.generation) {
      headers[CAPABILITY_HEADER] = identity.token;
    }
    return headers;
  }

  end(id: number): void { this.requests.delete(id); }

  install(session: Session): void {
    const filter: WebRequestFilter = { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] };
    session.webRequest.onBeforeRequest(filter, (details, callback) => callback({ cancel: !this.begin(details) }));
    session.webRequest.onBeforeSendHeaders(filter, (details, callback) => callback({ requestHeaders: this.headers(details, details.requestHeaders) }));
    session.webRequest.onCompleted(filter, (details) => this.end(details.id));
    session.webRequest.onErrorOccurred(filter, (details) => this.end(details.id));
  }
}
