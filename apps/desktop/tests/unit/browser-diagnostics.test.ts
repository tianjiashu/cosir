import { EventEmitter } from "node:events";
import fs from "node:fs";
import type { Session, WebContents } from "electron";
import { beforeEach, describe, expect, it, vi } from "vitest";

const partition = vi.hoisted(() => ({ netLog: { startLogging: vi.fn(), stopLogging: vi.fn(), currentlyLogging: false } }));
vi.mock("@/electron/main/desktop-log", () => ({ appendHostLogLine: vi.fn(), logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/paths", () => ({ appLogDir: () => "/tmp/logs" }));
vi.mock("@/electron/main/browser/browser-partition", () => ({ browserPartitionSession: () => partition }));
vi.mock("node:fs", () => ({ default: { mkdirSync: vi.fn(), statSync: vi.fn() } }));
import { appendHostLogLine, logDesktopEvent } from "@/electron/main/desktop-log";
import type { CdpSession } from "@/electron/main/browser/cdp-session";
import { observeCookieChanges } from "@/electron/main/browser/diagnostics/cookie-changes";
import { BrowserNetLogCapture } from "@/electron/main/browser/diagnostics/netlog-capture";
import { observeTabNetwork } from "@/electron/main/browser/diagnostics/tab-network";

function observer() {
  const events = new EventEmitter();
  const contents = Object.assign(new EventEmitter(), { debugger: new EventEmitter() });
  const send = vi.fn();
  observeTabNetwork({ on: events.on.bind(events), send } as unknown as CdpSession, contents as unknown as WebContents, { tabId: "t3", sessionId: 7 });
  return { events, contents, send };
}
const records = () => vi.mocked(appendHostLogLine).mock.calls.map(([, entry]) => entry);
const netLog = partition.netLog;
beforeEach(() => {
  vi.resetAllMocks();
  netLog.currentlyLogging = false;
  netLog.startLogging.mockImplementation(async () => { netLog.currentlyLogging = true; });
  netLog.stopLogging.mockImplementation(async () => { netLog.currentlyLogging = false; });
  vi.mocked(fs.statSync).mockReturnValue({ size: 0 } as fs.Stats);
});

describe("内置浏览器原生网络记录", () => {
  // 复现时手动开启；必须限制时长与大小、每次写新文件不覆盖上一次，结束（含到限自停）要让菜单刷新。
  it("captures on demand within bounds and reports when it ends", async () => {
    vi.useFakeTimers();
    try {
      const onChange = vi.fn();
      const capture = new BrowserNetLogCapture(onChange);
      expect(capture.active).toBe(false);
      vi.setSystemTime(new Date("2026-11-18T03:04:05Z"));
      capture.start();
      capture.start();
      expect(capture.active).toBe(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(netLog.startLogging).toHaveBeenCalledOnce();
      expect(netLog.startLogging).toHaveBeenCalledWith("/tmp/logs/browser-netlog-20261118-030405.json", { captureMode: "default" });
      await vi.advanceTimersByTimeAsync(15 * 60 * 1000);
      expect(netLog.stopLogging).toHaveBeenCalledOnce();
      expect(capture.active).toBe(false);
      expect(onChange).toHaveBeenCalledTimes(2);

      capture.start();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(netLog.startLogging.mock.calls[1][0]).not.toBe(netLog.startLogging.mock.calls[0][0]);
      vi.mocked(fs.statSync).mockReturnValue({ size: 20 * 1024 * 1024 } as fs.Stats);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(netLog.stopLogging).toHaveBeenCalledTimes(2);
      expect(capture.active).toBe(false);
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });

  // 开始失败只记原因，这次采集随之结束，菜单回到可再开始的状态。
  it("isolates start failures and leaves the capture restartable", async () => {
    vi.useFakeTimers();
    try {
      netLog.startLogging.mockRejectedValue(new Error("unavailable"));
      const capture = new BrowserNetLogCapture(vi.fn());
      capture.start();
      await vi.advanceTimersByTimeAsync(0);
      expect(logDesktopEvent).toHaveBeenCalledWith("WARNING", "browser", "browser_netlog_start_failed", expect.any(Object), expect.any(Error));
      expect(capture.active).toBe(false);
      expect(netLog.stopLogging).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });

  // 退出、用户停止与到限可能同时发生；只允许一份收尾，原生收尾不返回时不能无限拖住退出。
  it("shares concurrent stops, waits for startup, and bounds a hung flush", async () => {
    vi.useFakeTimers();
    try {
      let ready!: () => void;
      netLog.startLogging.mockImplementation(() => new Promise<void>((resolve) => { ready = () => { netLog.currentlyLogging = true; resolve(); }; }));
      netLog.stopLogging.mockImplementation(() => new Promise<void>(() => undefined));
      const capture = new BrowserNetLogCapture(vi.fn());
      capture.start();
      const stopping = capture.stop();
      expect(capture.stop()).toBe(stopping);
      expect(netLog.stopLogging).not.toHaveBeenCalled();
      ready();
      await vi.advanceTimersByTimeAsync(0);
      expect(netLog.stopLogging).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(5_000);
      await stopping;
      expect(logDesktopEvent).toHaveBeenCalledWith("WARNING", "browser", "browser_netlog_stop_failed", expect.any(Object), expect.objectContaining({ message: "network log flush timed out" }));
      expect(capture.active).toBe(false);
      expect(vi.getTimerCount()).toBe(0);
      await expect(new BrowserNetLogCapture(vi.fn()).stop()).resolves.toBeUndefined();
    } finally { vi.useRealTimers(); }
  });
});

describe("Cookie 变化诊断", () => {
  // 「登录成功却是游客」要分清 Cookie 是没存进去还是存进去后被覆盖、删除；绝不能把值写进日志。
  it("records why a cookie changed without its value", () => {
    const cookies = new EventEmitter();
    observeCookieChanges({ cookies } as unknown as Session);
    cookies.emit("changed", {}, { name: "web_session", value: "secret-session", domain: ".a.test", path: "/", hostOnly: false, secure: true, httpOnly: true, sameSite: "no_restriction", session: false, expirationDate: 99 }, "overwrite", true);
    expect(records().at(-1)).toMatchObject({ event: "browser_cookie_changed", data: { name: "web_session", domain: ".a.test", cause: "overwrite", removed: true, http_only: true } });
    expect(JSON.stringify(records())).not.toContain("secret-");
  });
});

describe("标签网络诊断", () => {
  // 取消、CORS 与连接失败不能再被同一个 canceled 字符串掩盖。
  it("retains the raw cancellation and CORS reason without issuing CDP commands", () => {
    const { events, send } = observer();
    events.emit("Network.loadingFailed", {
      requestId: "r", timestamp: 6.015, errorText: "net::ERR_ABORTED", canceled: true,
      blockedReason: "cors", corsErrorStatus: { corsError: "PreflightMissingAllowOriginHeader", failedParameter: "private-response-value" },
    }, "frame-2");
    expect(records().at(-1)?.data).toMatchObject({
      tab_id: "t3", session_id: 7, cdp_session_id: "frame-2", request_id: "r", timestamp: 6.015,
      error_text: "net::ERR_ABORTED", canceled: true, blocked_reason: "cors", cors_error: "PreflightMissingAllowOriginHeader",
    });
    expect(JSON.stringify(records())).not.toContain("private-response-value");
    expect(send).not.toHaveBeenCalled();
  });

  // ExtraInfo 可以早于开始或晚于结束，不能被在途表丢掉，也不能硬配给重定向后的当前跳。
  it("preserves event order, preflight association and redirect boundaries", () => {
    const { events } = observer();
    events.emit("Network.responseReceivedExtraInfo", { requestId: "r", statusCode: 302, headers: {}, blockedCookies: [] });
    events.emit("Network.requestWillBeSent", {
      requestId: "r", timestamp: 2, type: "XHR", request: { method: "GET", url: "https://a.test/new" },
      redirectHasExtraInfo: true, redirectResponse: { url: "https://a.test/old?secret=abc", status: 302 },
    });
    events.emit("Network.requestWillBeSent", {
      requestId: "preflight", timestamp: 2.1, type: "Other", request: { method: "OPTIONS", url: "https://a.test/new" },
      initiator: { type: "preflight", requestId: "r" },
    });
    events.emit("Network.loadingFinished", { requestId: "r", timestamp: 3, encodedDataLength: 4 });
    events.emit("Network.responseReceivedExtraInfo", { requestId: "r", statusCode: 200, headers: {}, blockedCookies: [] });
    const data = records().slice(1).map((entry) => entry.data);
    expect(data.map((entry) => entry.method)).toEqual(["responseReceivedExtraInfo", "requestWillBeSent", "requestWillBeSent", "loadingFinished", "responseReceivedExtraInfo"]);
    expect(data[1]).toMatchObject({ redirect_has_extra_info: true, redirect: { status: 302, url: "https://a.test/old" } });
    expect(data[2]).toMatchObject({ initiator_type: "preflight", initiator_request_id: "r" });
    expect(data[4].status).toBe(200);
  });

  // 登录诊断要看到 Cookie 是否被阻挡，但不能把账号令牌、请求体或 URL 凭据写入新日志。
  it("keeps useful cookie and timing metadata while excluding credentials", () => {
    const { events } = observer();
    const cookie = { name: "session", value: "secret-cookie", secure: true, httpOnly: true, sameSite: "None" };
    events.emit("Network.requestWillBeSent", {
      requestId: "r", timestamp: 1, request: { method: "POST", url: "https://user:password@a.test/login?token=secret-query#secret-fragment", postData: "secret-body", headers: { Cookie: "secret-cookie", Authorization: "secret-auth" } },
    });
    events.emit("Network.requestWillBeSentExtraInfo", { requestId: "r", headers: { Cookie: "secret-cookie" }, associatedCookies: [{ cookie, blockedReasons: ["ThirdPartyPhaseout"] }], connectTiming: { requestTime: 1.1 } });
    events.emit("Network.responseReceivedExtraInfo", {
      requestId: "r", statusCode: 200, headers: { "Set-Cookie": "session=secret-set; HttpOnly\nother=secret-other", "Access-Control-Allow-Origin": "https://a.test?token=secret-reflected" },
      blockedCookies: [{ cookie, cookieLine: "secret-line", blockedReasons: ["SameSiteNoneInsecure"] }],
    });
    events.emit("Network.responseReceived", {
      requestId: "r", timestamp: 2, hasExtraInfo: true,
      response: { url: "https://a.test/login?token=secret-query", status: 200, fromServiceWorker: true, timing: { requestTime: 1, dnsStart: -1, connectStart: 4, receiveHeadersEnd: 500, unexpected: "secret-unlisted" } },
    });
    expect(JSON.stringify(records())).not.toMatch(/secret-|password/);
    expect(records()[1].data.url).toBe("https://a.test/login");
    expect(records()[2].data.cookies).toEqual([{ name: "session", secure: true, http_only: true, same_site: "None", blocked_reasons: ["ThirdPartyPhaseout"] }]);
    expect(records()[3].data.set_cookie_names).toEqual(["session", "other"]);
    expect(records()[3].data.allow_origin).toBe("invalid origin");
    expect(records()[4].data).toMatchObject({ from_service_worker: true, has_extra_info: true, timing: { dnsStart: -1, connectStart: 4, receiveHeadersEnd: 500 } });
  });

  // 诊断异常必须止于旁路，不能阻断原来的请求记账；关闭与断开还必须能解释事件为何缺失。
  it("isolates projection failures and records observation boundaries", () => {
    const { events, contents } = observer();
    const business = vi.fn();
    events.on("Network.requestWillBeSent", business);
    expect(() => events.emit("Network.requestWillBeSent", { requestId: "r", request: { method: "GET", url: "invalid" } })).not.toThrow();
    expect(business).toHaveBeenCalledOnce();
    expect(logDesktopEvent).toHaveBeenCalledWith("WARNING", "browser", "browser_network_diagnostics_failed", { tab_id: "t3", session_id: 7 }, expect.any(TypeError));
    contents.debugger.emit("detach", {}, "target_closed");
    contents.emit("destroyed");
    expect(records().slice(-2).map((entry) => entry.event)).toEqual(["browser_network_observation_detached", "browser_network_observation_closed"]);
  });

  // 视频分片与图片会把当日日志轮转满、挤掉接口请求；但它们失败时仍要能说出是哪一个。
  it("skips successful static resources but records their failures with the address", () => {
    const { events } = observer();
    const image = { requestId: "img", timestamp: 1, type: "Image", request: { method: "GET", url: "https://cdn.test/a.png?sig=secret-sig" } };
    events.emit("Network.requestWillBeSent", image);
    events.emit("Network.requestWillBeSentExtraInfo", { requestId: "img", headers: {}, associatedCookies: [] });
    events.emit("Network.responseReceived", { requestId: "img", timestamp: 2, response: { url: "https://cdn.test/a.png", status: 200 } });
    events.emit("Network.loadingFinished", { requestId: "img", timestamp: 3, encodedDataLength: 9 });
    events.emit("Network.requestWillBeSent", { ...image, requestId: "img2" });
    events.emit("Network.loadingFailed", { requestId: "img2", timestamp: 4, type: "Image", errorText: "net::ERR_TIMED_OUT" });
    events.emit("Network.requestWillBeSent", { requestId: "api", timestamp: 5, type: "XHR", request: { method: "GET", url: "https://a.test/api" } });
    const data = records().slice(1).map((entry) => entry.data);
    expect(data.map((entry) => entry.request_id)).toEqual(["img2", "api"]);
    expect(data[0]).toMatchObject({ method: "loadingFailed", resource_type: "Image", url: "https://cdn.test/a.png", error_text: "net::ERR_TIMED_OUT" });
    expect(JSON.stringify(records())).not.toContain("secret-");
  });
});
