import type { Session, WebRequestFilter } from "electron";

export const CAPABILITY_HEADER = "X-Cosir-Capability";
export type BackendIdentity = { baseUrl: string; token: string; generation: number };
type RequestSource = { id: number; url: string; webContentsId?: number; frame?: { url: string; parent: unknown } | null; initiatorOrigin?: string };

/** 单一 hook 所有者：仅应用主 frame 的直连后端请求获得身份，重定向不能提权。 */
export class BackendRequestIdentity {
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

  begin(source: RequestSource): boolean {
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
