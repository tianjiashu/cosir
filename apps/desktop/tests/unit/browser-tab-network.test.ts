import { beforeEach, describe, expect, it, vi } from "vitest";

const contents = vi.hoisted(() => ({
  setBackgroundThrottling: vi.fn(), setWindowOpenHandler: vi.fn(), on: vi.fn(), once: vi.fn(), ipc: { on: vi.fn() },
  loadURL: vi.fn(), close: vi.fn(), getURL: () => "", getTitle: () => "",
  isLoading: () => false, isCrashed: () => false, isDestroyed: () => false, getOSProcessId: () => 1,
  debugger: { attach: vi.fn(), detach: vi.fn(), isAttached: vi.fn(), on: vi.fn(), sendCommand: vi.fn() },
}));
vi.mock("electron", () => ({
  WebContentsView: class { webContents = contents; setBounds() {} getBounds() { return { x: 0, y: 0, width: 0, height: 0 }; } },
}));
vi.mock("@/electron/main/browser/browser-partition", () => ({ browserPageWebPreferences: () => ({}) }));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { BrowserTab, type TabEvents } from "@/electron/main/browser/browser-tab";
import { browserError } from "@/electron/main/browser/errors";
import { BODY_LIMITS } from "@/electron/main/browser/tab-log";

const flush = async () => { for (let i = 0; i < 16; i += 1) await Promise.resolve(); };

/** 页面「停着」与否由测试控制，等同 `BrowserHost.pausedBy` 按进程给出的结论。 */
let paused: ReturnType<typeof browserError> | null = null;
let emit: (method: string, params: unknown) => void;

function events(): TabEvents {
  return {
    onWindowOpen: () => ({ action: "deny" }), onChange: vi.fn(), onGone: vi.fn(), onDialogOpened: vi.fn(), onDialogClosed: vi.fn(),
    pausedBy: () => paused, chooseFiles: vi.fn(async () => null), isAttached: () => false,
  };
}

async function openTab(): Promise<BrowserTab> {
  const tab = new BrowserTab({ tabId: "t1", sessionId: 1, createdBy: "agent", openerId: null, downloadDir: "/tmp", events: events() });
  await tab.initialize(new AbortController().signal);
  return tab;
}

/** 一条 Fetch 从开始到结束，经 CDP 事件派发（与真实页面同一入口）。 */
function finishFetch(id: string, mime = "application/json", encodedDataLength = 20): void {
  emit("Network.requestWillBeSent", { requestId: id, type: "Fetch", timestamp: 1, request: { method: "POST", url: `http://a/${id}` } });
  emit("Network.responseReceived", { requestId: id, timestamp: 1.1, response: { status: 500, mimeType: mime } });
  emit("Network.loadingFinished", { requestId: id, timestamp: 1.2, encodedDataLength });
}

const bodyCalls = () => contents.debugger.sendCommand.mock.calls.filter(([method]) => method === "Network.getResponseBody");

beforeEach(() => {
  vi.resetAllMocks();
  paused = null;
  contents.loadURL.mockResolvedValue(undefined);
  contents.debugger.sendCommand.mockResolvedValue({});
  let attached = false;
  contents.debugger.attach.mockImplementation(() => { attached = true; });
  contents.debugger.isAttached.mockImplementation(() => attached);
  contents.debugger.on.mockImplementation((event: string, handler: (...args: unknown[]) => void) => {
    if (event === "message") emit = (method, params) => handler({}, method, params, "");
  });
});

describe("BrowserTab 响应体采集", () => {
  // 目的：渲染进程留得住要采的正文；潜在缺陷：沿用旧的 1 KB 缓冲，超过 1 KB 的接口永远取不到。
  it("enables Network with the renderer buffer limits", async () => {
    await openTab();
    expect(contents.debugger.sendCommand).toHaveBeenCalledWith(
      "Network.enable", { maxTotalBufferSize: BODY_LIMITS.rendererTotalBytes, maxResourceBufferSize: BODY_LIMITS.rendererResourceBytes }, undefined,
    );
  });

  // 目的：只为文本类 Fetch / XHR 发采集；潜在缺陷：图片、文档也去取正文，白占缓冲与往返。
  it("captures text Fetch bodies, decodes base64, and skips binary and non-Fetch requests", async () => {
    const tab = await openTab();
    contents.debugger.sendCommand.mockImplementation(async (method: string, params: { requestId?: string }) => {
      if (method !== "Network.getResponseBody") return {};
      return params.requestId === "b64"
        ? { body: Buffer.from('{"中":1}').toString("base64"), base64Encoded: true }
        : { body: '{"error":"quota exceeded"}', base64Encoded: false };
    });
    finishFetch("save");
    finishFetch("b64");
    finishFetch("png", "image/png");
    emit("Network.requestWillBeSent", { requestId: "doc", type: "Document", timestamp: 1, request: { method: "GET", url: "http://a/" } });
    emit("Network.loadingFinished", { requestId: "doc", timestamp: 1.2, encodedDataLength: 10 });
    await flush();
    expect(bodyCalls().map(([, params]) => params)).toEqual([{ requestId: "save" }, { requestId: "b64" }]);
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "text", text: '{"error":"quota exceeded"}' });
    expect(tab.log.readBody(2)!.body).toMatchObject({ state: "text", text: '{"中":1}' });
    expect(tab.log.readBody(3)!.body).toMatchObject({ state: "binary", mime: "image/png" });
  });

  // 目的：后台采集不归属 Agent 的动作；潜在缺陷：动作期间结束的请求进了操作的记账，拖住收尾、甚至让标签变成「结果未知」。
  it("keeps captures out of the current operation's command tracking", async () => {
    const tab = await openTab();
    const track = vi.fn();
    tab.operation = { track } as never;
    contents.debugger.sendCommand.mockImplementation(async (method: string) => (method === "Network.getResponseBody" ? { body: "{}", base64Encoded: false } : {}));
    finishFetch("during-act");
    await tab.session.send("DOM.getDocument");
    await flush();
    expect(bodyCalls()).toHaveLength(1);
    expect(track).toHaveBeenCalledTimes(1);
  });

  // 目的：核心场景「拿到响应就 alert」也能读到正文；潜在缺陷：采集被弹窗挡住就永久丢失。
  it("defers a capture blocked by a dialog and retries it when the dialog is answered", async () => {
    const tab = await openTab();
    paused = browserError("DIALOG_OPEN", "the page is paused: it is showing an alert");
    finishFetch("save");
    await flush();
    expect(bodyCalls()).toHaveLength(0);
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "deferred", text: null });

    paused = null;
    contents.debugger.sendCommand.mockImplementation(async (method: string) => (method === "Network.getResponseBody" ? { body: '{"error":"x"}', base64Encoded: false } : {}));
    tab.resumeAfterDialog();
    await flush();
    expect(bodyCalls()).toEqual([["Network.getResponseBody", { requestId: "save" }, undefined]]);
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "text", text: '{"error":"x"}' });
  });

  // 目的：在途的采集遇到弹窗同样补采；潜在缺陷：只处理「发之前就停着」，被中断的在途命令被记成采集失败。
  it("defers a capture interrupted by a dialog while in flight", async () => {
    const tab = await openTab();
    contents.debugger.sendCommand.mockImplementation((method: string) => (method === "Network.getResponseBody" ? new Promise(() => undefined) : Promise.resolve({})));
    finishFetch("save");
    await flush();
    tab.interruptCdp(browserError("DIALOG_OPEN", "the page is paused"));
    await flush();
    expect(tab.log.readBody(1)!.body.state).toBe("deferred");
  });

  // 目的：补采前页面已离开要如实说明；潜在缺陷：失败被吞掉，条目永远停在「采集中」。
  it("records why a capture failed when the browser no longer has the body", async () => {
    const tab = await openTab();
    contents.debugger.sendCommand.mockImplementation(async (method: string) => {
      if (method === "Network.getResponseBody") throw new Error("No resource with given identifier found");
      return {};
    });
    finishFetch("gone");
    await flush();
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "unavailable", reason: expect.stringContaining("No resource with given identifier found") });
  });

  // 目的：XHR 与 Fetch 同等对待；潜在缺陷：只认 Fetch，老式 XMLHttpRequest 的接口永远采不到。
  it("captures XHR bodies too", async () => {
    const tab = await openTab();
    contents.debugger.sendCommand.mockImplementation(async (method: string) => (method === "Network.getResponseBody" ? { body: "ok", base64Encoded: false } : {}));
    emit("Network.requestWillBeSent", { requestId: "x", type: "XHR", timestamp: 1, request: { method: "GET", url: "http://a/x" } });
    emit("Network.responseReceived", { requestId: "x", timestamp: 1.1, response: { status: 200, mimeType: "text/plain" } });
    emit("Network.loadingFinished", { requestId: "x", timestamp: 1.2, encodedDataLength: 2 });
    await flush();
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "text", text: "ok" });
  });

  // 目的：采集是后台读取，等不到就放手；潜在缺陷：渲染进程卡住时采集一直挂着，条目永远「采集中」、计时器不释放。
  it("gives up a capture after its timeout and leaves no timer behind", async () => {
    vi.useFakeTimers();
    try {
      const tab = await openTab();
      contents.debugger.sendCommand.mockImplementation((method: string) => (method === "Network.getResponseBody" ? new Promise(() => undefined) : Promise.resolve({})));
      finishFetch("stuck");
      await flush();
      expect(tab.log.readBody(1)!.body.state).toBe("pending");
      await vi.advanceTimersByTimeAsync(5_000);
      expect(tab.log.readBody(1)!.body).toMatchObject({ state: "unavailable", reason: expect.stringContaining("did not respond") });
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  // 目的：标签关掉后采集的迟到失败不出事；潜在缺陷：迟到拒绝成为未处理异常。
  it("tolerates a capture that fails after the tab is closed", async () => {
    const tab = await openTab();
    let fail!: (error: Error) => void;
    contents.debugger.sendCommand.mockImplementation((method: string) => (method === "Network.getResponseBody" ? new Promise((_, reject) => { fail = reject; }) : Promise.resolve({})));
    finishFetch("late");
    await flush();
    tab.close();
    fail(new Error("Target closed"));
    await flush();
    expect(tab.log.readBody(1)!.body.state).toBe("unavailable");
  });

  // 目的：补采时页面已离开要如实说明；潜在缺陷：补采失败被吞掉，条目永远停在「采集中」。
  it("records an unavailable body when the retry after a dialog fails", async () => {
    const tab = await openTab();
    paused = browserError("DIALOG_OPEN", "the page is paused");
    finishFetch("moved");
    await flush();
    paused = null;
    contents.debugger.sendCommand.mockImplementation(async (method: string) => {
      if (method === "Network.getResponseBody") throw new Error("No resource with given identifier found");
      return {};
    });
    tab.resumeAfterDialog();
    await flush();
    expect(tab.log.readBody(1)!.body).toMatchObject({ state: "unavailable", reason: expect.stringContaining("No resource") });
  });

  // 目的：「弹窗之后的恢复」原有两件事不变；潜在缺陷：改名扩职责时漏掉设备模拟补设或操作收尾。
  it("still resyncs emulation and resumes the operation's cleanup after a dialog", async () => {
    const tab = await openTab();
    const resync = vi.spyOn(tab.emulation, "resync");
    const resumeCleanup = vi.fn();
    tab.operation = { track: vi.fn(), resumeCleanup } as never;
    tab.resumeAfterDialog();
    expect(resync).toHaveBeenCalledTimes(1);
    expect(resumeCleanup).toHaveBeenCalledTimes(1);
  });
});
