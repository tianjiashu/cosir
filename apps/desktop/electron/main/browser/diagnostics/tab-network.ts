import type { WebContents } from "electron";

import { logDesktopEvent } from "../../desktop-log";
import type { CdpSession } from "../cdp-session";
import { writeNetworkRecord } from "./network-record";

type Fields = Record<string, unknown>;
type Headers = Record<string, string>;
type Response = {
  url: string; status: number; protocol?: string; remoteIPAddress?: string;
  connectionId?: number; connectionReused?: boolean; fromDiskCache?: boolean; fromServiceWorker?: boolean;
  timing?: Record<string, number>;
};
type PartitionKey = { topLevelSite: string; hasCrossSiteAncestor: boolean };
type Cookie = { name: string; secure: boolean; httpOnly: boolean; sameSite?: string; domain?: string; path?: string; partitionKey?: PartitionKey; partitionKeyOpaque?: boolean };

/**
 * 静态资源只在失败时记：视频分片、图片会在几小时内把当日日志轮转满，挤掉真正要看的文档与接口请求。
 * 记住被略过请求的地址，失败时仍能说出是哪一个；超过上限丢最早的，迟到的 ExtraInfo 因此可能照常落盘，只多几行噪音。
 */
const QUIET_RESOURCE_TYPES = new Set(["Image", "Media", "Font", "Stylesheet"]);
const QUIET_REQUEST_LIMIT = 2_000;

/**
 * 常驻的标签网络诊断旁路：只订阅已有 CDP 事件，不发命令、不截获请求、不消费 TabLog（Agent 看的标签日志）。
 * 原始事件独立落盘，按 tab / CDP session / request id 关联；ExtraInfo 可能乱序到达，重定向还会复用 id，
 * 因此这里不猜测它属于哪一跳。只覆盖开了 Network 的主会话：跨进程子框架、Worker 与 Service Worker 自己发的请求不在其中
 * （`page-frames.ts`）。字段白名单：不记录认证值、Cookie 值、正文或 URL 查询参数。
 *
 * 事件分发里抛出的异常会成为 main 进程的未捕获异常，所以投影与落盘在 `guarded` 一处兜住、记下原因，不再向外抛。
 */
export function observeTabNetwork(session: CdpSession, contents: WebContents, identity: { tabId: string; sessionId: number }): void {
  const write = (event: string, data: Fields, cdpSessionId?: string) => writeNetworkRecord(event, {
    tab_id: identity.tabId, session_id: identity.sessionId, cdp_session_id: cdpSessionId ?? null, ...data,
  });
  const guarded = (action: () => void) => {
    try { action(); } catch (error) {
      logDesktopEvent("WARNING", "browser", "browser_network_diagnostics_failed", { tab_id: identity.tabId, session_id: identity.sessionId }, error);
    }
  };
  const quiet = new Map<string, string>();
  const on = <T extends { requestId: string; timestamp?: number }>(method: string, project: (params: T) => Fields | null) => {
    session.on<T>(`Network.${method}`, (params, cdpSessionId) => guarded(() => {
      const fields = project(params);
      if (fields !== null) write("browser_network_event", { method, request_id: params.requestId, timestamp: params.timestamp ?? null, ...fields }, cdpSessionId);
    }));
  };
  on<{ requestId: string; timestamp: number; frameId?: string; loaderId: string; type?: string; request: { method: string; url: string; headers?: Headers }; initiator?: { type: string; requestId?: string }; redirectResponse?: Response; redirectHasExtraInfo?: boolean }>("requestWillBeSent", (p) => {
    if (p.type !== undefined && QUIET_RESOURCE_TYPES.has(p.type)) {
      quiet.set(p.requestId, diagnosticUrl(p.request.url));
      if (quiet.size > QUIET_REQUEST_LIMIT) quiet.delete(quiet.keys().next().value!);
      return null;
    }
    return {
      resource_type: p.type, http_method: p.request.method, url: diagnosticUrl(p.request.url),
      frame_id: p.frameId, loader_id: p.loaderId,
      origin: originHeader(p.request.headers, "origin"),
      initiator_type: p.initiator?.type, initiator_request_id: p.initiator?.requestId,
      redirect_has_extra_info: p.redirectHasExtraInfo, redirect: p.redirectResponse && responseFields(p.redirectResponse),
    };
  });
  on<{ requestId: string; timestamp: number; response: Response; hasExtraInfo?: boolean }>("responseReceived", (p) => quiet.has(p.requestId) ? null : ({
    ...responseFields(p.response), has_extra_info: p.hasExtraInfo,
  }));
  on<{ requestId: string; timestamp: number; type?: string; errorText: string; canceled?: boolean; blockedReason?: string; corsErrorStatus?: { corsError: string; failedParameter: string } }>("loadingFailed", (p) => {
    const quietUrl = quiet.get(p.requestId);
    quiet.delete(p.requestId);
    return {
      resource_type: p.type, url: quietUrl,
      error_text: p.errorText, canceled: p.canceled ?? false, blocked_reason: p.blockedReason,
      cors_error: p.corsErrorStatus?.corsError,
      // failedParameter 可能带服务端返回的整段内容；错误枚举已足以区分 CORS 失败种类。
    };
  });
  on<{ requestId: string; timestamp: number; encodedDataLength: number }>("loadingFinished", (p) => {
    if (quiet.delete(p.requestId)) return null;
    return { encoded_data_length: p.encodedDataLength };
  });
  on<{ requestId: string; headers: Headers; associatedCookies: { cookie: Cookie; blockedReasons: string[] }[]; connectTiming?: { requestTime: number }; siteHasCookieInOtherPartition?: boolean }>("requestWillBeSentExtraInfo", (p) => quiet.has(p.requestId) ? null : ({
    origin: originHeader(p.headers, "origin"), preflight_method: header(p.headers, "access-control-request-method"),
    preflight_headers: header(p.headers, "access-control-request-headers"), request_time: p.connectTiming?.requestTime,
    cookies: p.associatedCookies.slice(0, 64).map(({ cookie, blockedReasons }) => ({ ...cookieFields(cookie), blocked_reasons: blockedReasons })),
    cookie_count: p.associatedCookies.length, site_has_cookie_in_other_partition: p.siteHasCookieInOtherPartition,
  }));
  on<{ requestId: string; statusCode: number; headers: Headers; blockedCookies: { blockedReasons: string[]; cookie?: Cookie }[]; cookiePartitionKey?: PartitionKey; cookiePartitionKeyOpaque?: boolean }>("responseReceivedExtraInfo", (p) => quiet.has(p.requestId) ? null : ({
    status: p.statusCode,
    allow_origin: originHeader(p.headers, "access-control-allow-origin"), allow_credentials: header(p.headers, "access-control-allow-credentials"),
    allow_methods: header(p.headers, "access-control-allow-methods"), allow_headers: header(p.headers, "access-control-allow-headers"),
    // 只取名称，绝不把 Set-Cookie 值或完整 cookieLine 落盘。空 blocked 列表不代表 Cookie 已保存：存没存看 Cookie 变化记录。
    set_cookie_names: Object.entries(p.headers).find(([key]) => key.toLowerCase() === "set-cookie")?.[1]
      .split("\n").slice(0, 64).map((line) => /^\s*([^=;\s]+)=/.exec(line)?.[1]?.slice(0, 128)).filter(Boolean),
    blocked_cookies: p.blockedCookies.slice(0, 64).map(({ cookie, blockedReasons }) => ({ ...(cookie && cookieFields(cookie)), blocked_reasons: blockedReasons })),
    blocked_cookie_count: p.blockedCookies.length, cookie_partition_key: p.cookiePartitionKey && partitionFields(p.cookiePartitionKey), cookie_partition_key_opaque: p.cookiePartitionKeyOpaque,
  }));
  session.on<{ frame: { id: string; loaderId: string; url: string } }>("Page.frameNavigated", (p, cdpSessionId) => guarded(() => write("browser_network_frame_navigated", {
    frame_id: p.frame.id, loader_id: p.frame.loaderId, url: diagnosticUrl(p.frame.url),
  }, cdpSessionId)));
  session.on<{ sessionId: string; targetInfo: { targetId: string; type: string } }>("Target.attachedToTarget", (p, parent) => guarded(() => write("browser_network_target_attached", {
    child_session_id: p.sessionId, target_id: p.targetInfo.targetId, target_type: p.targetInfo.type,
  }, parent)));
  session.on<{ sessionId: string }>("Target.detachedFromTarget", (p, parent) => guarded(() => write("browser_network_target_detached", { child_session_id: p.sessionId }, parent)));
  contents.on("did-start-navigation", (details) => guarded(() => write("browser_network_navigation", {
    url: diagnosticUrl(details.url), main_frame: details.isMainFrame, same_document: details.isSameDocument,
  })));
  contents.on("render-process-gone", (_event, details) => guarded(() => write("browser_network_renderer_gone", { reason: details.reason })));
  contents.debugger.on("detach", (_event, reason) => guarded(() => write("browser_network_observation_detached", { reason })));
  contents.once("destroyed", () => guarded(() => write("browser_network_observation_closed", {})));
  guarded(() => write("browser_network_observation_started", { coverage: "main CDP session only", quiet_resource_types: [...QUIET_RESOURCE_TYPES] }));
}

function diagnosticUrl(value: string): string {
  const url = new URL(value);
  // 非 HTTP 资源只留协议，避免 data URL、本机路径和扩展探测的载荷进入诊断文件。
  return url.protocol === "https:" || url.protocol === "http:" ? `${url.origin}${url.pathname}`.slice(0, 500) : url.protocol;
}

function header(headers: Headers | undefined, name: string): string | undefined {
  return Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === name)?.[1].slice(0, 500);
}

/** 错误的 CORS 响应头也可能包含 URL 凭据；保留「非法」这一诊断事实，不保存原值。 */
function originHeader(headers: Headers | undefined, name: string): string | undefined {
  const value = header(headers, name);
  return value === undefined ? undefined : safeOrigin(value);
}

/** 来源值只在本身就是一个合法来源（或 `*` / `null`）时原样记下，否则只记「非法」。 */
function safeOrigin(value: string): string {
  if (value === "*" || value === "null") return value;
  try {
    const url = new URL(value);
    return url.origin === value ? value : "invalid origin";
  } catch { return "invalid origin"; }
}

function cookieFields(cookie: Cookie): Fields {
  return {
    name: cookie.name.slice(0, 128), secure: cookie.secure, http_only: cookie.httpOnly, same_site: cookie.sameSite,
    domain: cookie.domain, path: cookie.path?.slice(0, 500), partition_key: cookie.partitionKey && partitionFields(cookie.partitionKey), partition_key_opaque: cookie.partitionKeyOpaque,
  };
}

function partitionFields(key: PartitionKey): Fields {
  return { top_level_site: safeOrigin(key.topLevelSite), has_cross_site_ancestor: key.hasCrossSiteAncestor };
}

function responseFields(response: Response): Fields {
  return {
    url: diagnosticUrl(response.url), status: response.status, protocol: response.protocol,
    remote_ip_address: response.remoteIPAddress, connection_id: response.connectionId, connection_reused: response.connectionReused,
    from_disk_cache: response.fromDiskCache, from_service_worker: response.fromServiceWorker,
    timing: response.timing && Object.fromEntries([
      "requestTime", "proxyStart", "proxyEnd", "dnsStart", "dnsEnd", "connectStart", "connectEnd", "sslStart", "sslEnd",
      "workerStart", "workerReady", "workerFetchStart", "workerRespondWithSettled", "sendStart", "sendEnd", "receiveHeadersStart", "receiveHeadersEnd",
    ].filter((key) => response.timing![key] !== undefined).map((key) => [key, response.timing![key]])),
  };
}
