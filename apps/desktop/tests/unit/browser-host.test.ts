import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TabEvents, TabOptions } from "@/electron/main/browser/browser-tab";
import { abortable } from "@/electron/main/browser/abortable";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/browser/browser-tab", () => ({
  get BrowserTab() { return FakeTab; },
}));
vi.mock("@/electron/main/browser/browser-partition", () => ({ browserPageWebPreferences: () => ({}) }));
import { deadlineMs, withinDeadline } from "@/electron/main/browser/deadlines";
import { BrowserHost } from "@/electron/main/browser/browser-host";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";

class FakeTab {
  static instances: FakeTab[] = [];
  finish!: () => void;
  fail!: (error: Error) => void;
  pending = new Promise<void>((resolve, reject) => { this.finish = resolve; this.fail = reject; });
  close = vi.fn();
  operation = null;
  initialized = true;
  documentGeneration = 0;
  modify = vi.fn(async (signal: AbortSignal, run: (operation: { signal: AbortSignal }) => Promise<unknown>) => run({ signal }));
  readonly view = {};
  show = vi.fn();
  hide = vi.fn();
  navigate = vi.fn().mockResolvedValue(undefined);
  act = vi.fn();
  snapshot = vi.fn().mockResolvedValue({ document: 0, outline: { entries: [{ key: ":1", depth: 0, text: "text 'page'", parent: -1, visible: true }], frames: 0 }, dialog: null });
  deliver = vi.fn().mockReturnValue(null);
  readonly contents = { isDestroyed: () => false };
  dialog: { id: number; type: "confirm"; message: string; defaultText: string } | null = null;
  processId = 1;
  readonly pausedSignal = new AbortController().signal;
  interruptCdp = vi.fn();
  readonly log = {
    note: vi.fn(),
    undeliveredNotes: vi.fn((): string[] => []),
    digest: vi.fn(() => ({ notes: [], newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 } })),
  };
  readonly status = null;
  readonly currentEmulation = { width: 1280, height: 800, mobile: false, colorScheme: null };
  readonly tabId: string; readonly taskId: number; readonly events: TabEvents; readonly options: TabOptions;
  get openerId() { return this.options.openerId; }
  get downloadDir() { return this.options.downloadDir; }
  get createdBy() { return this.options.createdBy; }
  constructor(options: TabOptions) {
    this.options = options;
    this.tabId = options.tabId; this.taskId = options.taskId; this.events = options.events;
    FakeTab.instances.push(this);
  }
  initialize(signal: AbortSignal) { return abortable(this.pending, signal); }
  summary() {
    return { tabId: this.tabId, taskId: this.taskId, createdBy: this.createdBy, openerId: this.openerId, url: "", title: "", loading: true, crashed: false, dialog: null };
  }
}

const context = (perTask = 2, total = 3) => ({ limits: { perTask, total }, downloadDir: "/workspace/.cosir/Download" });
const limits = context();
const tab = (index = 0) => FakeTab.instances[index];
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
beforeEach(() => { FakeTab.instances = []; });

describe("BrowserHost execution receipt budget", () => {
  it("returns execution before the total deadline even when draining and observation use their full budgets", async () => {
    vi.useFakeTimers();
    const host = new BrowserHost(() => null);
    try {
      const opening = host.userOpen(1, limits);
      tab().finish(); await opening;
      const execution = { steps: [{ status: "unknown", target: null }], interruption: { code: "DEADLINE_EXCEEDED", message: "budget" } };
      tab().act.mockImplementation(async (_actions, signal: AbortSignal) => {
        await new Promise<void>(resolve => signal.addEventListener("abort", () => setTimeout(resolve, 1000), { once: true }));
        return execution;
      });
      const snapshot = await tab().snapshot();
      let resolveSnapshot!: (value: typeof snapshot) => void;
      tab().snapshot.mockImplementation(() => new Promise(resolve => { resolveSnapshot = resolve; }));
      const result = withinDeadline("browser.act", new AbortController().signal, signal => host.act({ taskId: 1, context: limits, actions: [{ type: "wait_for", seconds: 1 }] }, signal));
      await vi.advanceTimersByTimeAsync(deadlineMs("browser.act") - 500);
      await expect(result).resolves.toMatchObject({ execution, observation: null, observationError: { code: "DEADLINE_EXCEEDED" } });
      resolveSnapshot(snapshot); await flush();
      expect(tab().deliver).not.toHaveBeenCalled(); expect(tab().log.digest).not.toHaveBeenCalled();
    } finally { host.closeAll(); vi.useRealTimers(); }
  });
});

describe("BrowserHost 资源生命周期", () => {
  it("初始化中即可列出标签；快照等待初始化，且不跨 Task 访问", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits })).toMatchObject([{ tabId: "t1", current: true }]);
    const snapshot = host.snapshot({ taskId: 1, view: {}, context: limits }, new AbortController().signal);
    await expect(host.snapshot({ taskId: 2, tab: "t1", view: {}, context: limits }, new AbortController().signal)).rejects.toMatchObject({ code: "TAB_NOT_FOUND" });
    expect(tab().snapshot).not.toHaveBeenCalled();
    tab().finish();
    await expect(opening).resolves.toBe("t1");
    await expect(snapshot).resolves.toMatchObject({ tabId: "t1", snapshot: "- text 'page'" });
  });

  // 目的：面板能标出「Agent 此刻正在执行」；潜在缺陷：计数在失败或取消时没减回去，标记一直亮着。
  it("标签在 Agent 操作执行期间标为忙碌，结束（含失败）后恢复并推送", async () => {
    const host = new BrowserHost(() => null);
    const states: boolean[] = [];
    host.onChange((state) => states.push(state.tabs[0]?.agentBusy ?? false));
    const opening = host.userOpen(1, limits);
    tab().finish();
    await opening;
    let release!: () => void;
    tab().snapshot.mockReturnValueOnce(new Promise((resolve) => { release = () => resolve({ document: 0, outline: null, dialog: null }); }));
    const reading = host.snapshot({ taskId: 1, view: {}, context: limits }, new AbortController().signal);
    await flush();
    expect(host.state().tabs[0].agentBusy).toBe(true);
    release();
    await reading;
    expect(host.state().tabs[0].agentBusy).toBe(false);
    tab().snapshot.mockRejectedValueOnce(new Error("boom"));
    await expect(host.snapshot({ taskId: 1, view: {}, context: limits }, new AbortController().signal)).rejects.toThrow("boom");
    expect(host.state().tabs[0].agentBusy).toBe(false);
    expect(states.slice(-4)).toEqual([true, false, true, false]);
  });

  it("初始化中的标签计入每 Task 与全局上限", async () => {
    const host = new BrowserHost(() => null);
    const openings = [host.userOpen(1, limits), host.userOpen(1, limits), host.userOpen(2, limits)];
    await expect(host.userOpen(1, limits)).rejects.toThrow(/per-task/);
    await expect(host.userOpen(3, limits)).rejects.toThrow(/global/);
    FakeTab.instances.forEach((item) => item.finish());
    await Promise.all(openings);
  });

  it.each(["task", "user", "shutdown"])("%s 关闭初始化中的标签，迟到完成不复活", async (kind) => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    const rejected = expect(opening).rejects.toMatchObject({ code: "TAB_NOT_FOUND" });
    await flush();
    if (kind === "task") host.dropTasks([1]);
    else if (kind === "user") host.userClose("t1");
    else host.closeAll();
    await rejected;
    expect(tab().close).toHaveBeenCalledTimes(1);
    tab().finish();
    await flush();
    expect(host.state().tabs).toEqual([]);
    expect(tab().close).toHaveBeenCalledTimes(1);
    if (kind === "shutdown") await expect(host.userOpen(1, limits)).rejects.toMatchObject({ code: "UNAVAILABLE" });
  });

  // 潜在缺陷：用户的收尾操作常与关闭竞争（面板卸载时摘下画面、连点关闭、标签关掉后再点弹窗提示条），按「必须在」查
  // 标签就报假失败、弹出「关闭标签页失败」；而要在标签上产出结果的入口仍须报标签不在。
  it("摘下画面、关闭与作答弹窗对已关闭的标签什么也不做，摆上画面仍报标签不在", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    tab().finish();
    await opening;
    host.userClose("t1");
    expect(() => host.userClose("t1")).not.toThrow();
    expect(() => host.place("t1", null)).not.toThrow();
    expect(() => host.userAnswerDialog("t1", 3, { accept: true })).not.toThrow();
    expect(() => host.revealDownload("d9")).not.toThrow();
    expect(() => host.place("t1", { x: 0, y: 0, width: 100, height: 100 })).toThrow(expect.objectContaining({ code: "TAB_NOT_FOUND" }));
    expect(tab().close).toHaveBeenCalledTimes(1);
  });

  // 潜在缺陷：「在不在面板上」另由设备模拟的占位矩形推断，某条只摘视图的路径（如关闭）没清它，后台标签里网页自己
  // 打开的选文件框就会弹给看不见这个页面的用户。
  it("只有此刻挂在面板上的标签算挂上，摆上新的、摘下或关闭后都不算", async () => {
    const window = {
      contentView: { addChildView: vi.fn(), removeChildView: vi.fn() },
      webContents: { getZoomFactor: () => 1 },
    };
    const host = new BrowserHost(() => window as never);
    const openings = [host.userOpen(1, limits), host.userOpen(1, limits)];
    tab(0).finish();
    tab(1).finish();
    await Promise.all(openings);
    const rect = { x: 0, y: 0, width: 400, height: 300 };
    const attached = () => FakeTab.instances.map((item) => item.events.isAttached());

    host.place("t1", rect);
    expect(attached()).toEqual([true, false]);
    host.place("t2", rect);
    expect(attached()).toEqual([false, true]);
    host.place("t2", null);
    expect(attached()).toEqual([false, false]);
    host.place("t1", rect);
    host.userClose("t1");
    expect(attached()).toEqual([false, false]);
  });

  it("初始化失败回收视图和名额，后续标签不复用 id", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, context(1, 1));
    const rejected = expect(opening).rejects.toThrow("initialization failed");
    tab().fail(new Error("initialization failed"));
    await rejected;
    expect(tab().close).toHaveBeenCalledTimes(1);
    expect(host.state().tabs).toEqual([]);
    const next = host.userOpen(1, context(1, 1));
    tab(1).finish();
    await expect(next).resolves.toBe("t2");
  });

  it("新建请求取消回收资源；已有标签的等待者取消不关闭共享标签", async () => {
    const host = new BrowserHost(() => null);
    const owner = new AbortController();
    const opening = host.navigate({ taskId: 1, context: limits, newTab: true, target: { url: "https://example.com" } }, owner.signal);
    const rejected = expect(opening).rejects.toMatchObject({ name: "AbortError" });
    owner.abort();
    await rejected;
    expect(tab().close).toHaveBeenCalledTimes(1);

    const next = host.userOpen(1, limits);
    const waiter = new AbortController();
    const waiting = host.snapshot({ taskId: 1, view: {}, context: limits }, waiter.signal);
    const cancelled = expect(waiting).rejects.toMatchObject({ name: "AbortError" });
    waiter.abort();
    await cancelled;
    expect(tab(1).close).not.toHaveBeenCalled();
    tab(1).finish();
    await next;
  });

  it("页面开出的标签沿用来源的 Task 与下载目录、不抢当前标签；自己关掉时注销并告诉来源", async () => {
    const host = new BrowserHost(() => null);
    const first = host.userOpen(1, context(5, 5));
    tab().finish(); await first;
    const response = tab().events.onWindowOpen("https://login.example.com");
    expect(response).toMatchObject({ action: "allow", outlivesOpener: true });
    const adopted = { id: "popup-contents" };
    const returned = (response as { createWindow: (options: unknown) => unknown }).createWindow({ webContents: adopted });
    expect(tab(1).options).toMatchObject({ taskId: 1, openerId: "t1", downloadDir: context().downloadDir, adopted });
    expect(returned).toBe(tab(1).contents);
    expect(tab().log.note).toHaveBeenCalledWith(expect.stringContaining("in a new tab t2"));
    tab(1).finish(); await flush();
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits }).find((item) => item.current)?.tabId).toBe("t1");
    tab(1).log.undeliveredNotes.mockReturnValue(["the download \"a.csv\" finished"]);
    tab(1).events.onGone();
    expect(host.state().tabs.map((item: { tabId: string }) => item.tabId)).toEqual(["t1"]);
    // 未读备注（如下载结果）转给来源标签，不随标签消失。
    expect(tab().log.note).toHaveBeenCalledWith('in tab t2: the download "a.csv" finished');
    expect(tab(1).close).toHaveBeenCalledTimes(1);
    expect(tab().log.note).toHaveBeenLastCalledWith("tab t2 (opened by this page) closed itself");
  });

  it("非网页地址、超出上限与宿主退出后都拒绝开窗", async () => {
    const host = new BrowserHost(() => null);
    const first = host.userOpen(1, context(1, 5));
    tab().finish(); await first;
    expect(tab().events.onWindowOpen("file:///etc/passwd")).toEqual({ action: "deny" });
    expect(tab().events.onWindowOpen("https://example.com")).toEqual({ action: "deny" });
    expect(tab().log.note).toHaveBeenLastCalledWith(expect.stringContaining("per-task limit"));
    host.closeAll();
    expect(tab().events.onWindowOpen("https://example.com")).toEqual({ action: "deny" });
    expect(FakeTab.instances).toHaveLength(1);
  });

  it("关闭当前标签回落到最新标签", async () => {
    const host = new BrowserHost(() => null);
    const openings = [host.userOpen(1, context(5, 5)), host.userOpen(1, context(5, 5)), host.userOpen(1, context(5, 5))];
    FakeTab.instances.forEach((item) => item.finish());
    await Promise.all(openings);
    host.userClose("t3");
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits }).find((item) => item.current)?.tabId).toBe("t2");
  });

  // 目的：弹窗停住的是整个渲染进程；潜在缺陷：同进程的另一个标签 CDP 空等 10 秒报「无响应」，Agent 不知道该去哪答。
  it("pauses every tab sharing the dialog's process and names the tab to answer", async () => {
    const host = new BrowserHost(() => null);
    const openings = [host.userOpen(1, context(5, 5)), host.userOpen(1, context(5, 5)), host.userOpen(2, context(5, 5))];
    FakeTab.instances.forEach((item) => item.finish());
    await Promise.all(openings);
    tab(2).processId = 2;
    tab(0).dialog = { id: 9, type: "confirm", message: "sure?", defaultText: "" };
    tab(0).events.onDialogOpened();
    expect(tab(0).interruptCdp).toHaveBeenCalledWith(expect.objectContaining({ code: "DIALOG_OPEN" }));
    expect(tab(1).interruptCdp).toHaveBeenCalledTimes(1);
    expect(tab(2).interruptCdp).not.toHaveBeenCalled();
    expect(tab(1).events.pausedBy()?.message).toMatch(/tab t1 \(this task\) shares its browser process and is showing a confirm dialog \(#9\)/);
    expect(tab(2).events.pausedBy()).toBeNull();
  });

  // 目的：下载归 Task 不归标签；潜在缺陷：弹出页下载后自己关闭，下载随标签一起被取消，结果也没人收到。
  it("keeps a download going after its tab closes itself and reports it on the task's current tab", async () => {
    const host = new BrowserHost(() => null);
    const first = host.userOpen(1, { ...context(5, 5), downloadDir: fs.mkdtempSync(path.join(os.tmpdir(), "cosir-host-dl-")) });
    tab().finish(); await first;
    const response = tab().events.onWindowOpen("https://example.com/export") as { createWindow: (options: unknown) => unknown };
    response.createWindow({ webContents: {} });
    tab(1).finish(); await flush();
    const item = Object.assign(new EventEmitter(), {
      cancelled: false,
      getFilename: () => "report.csv", getTotalBytes: () => 1, getReceivedBytes: () => 1, getState: () => "progressing",
      setSavePath: vi.fn(), cancel() { this.cancelled = true; },
    });
    host.download(tab(1).contents as never, item as never);
    tab(1).events.onGone();
    expect(item.cancelled).toBe(false);
    item.emit("done", {}, "completed");
    expect(host.state().downloads).toMatchObject([{ taskId: 1, tabId: "t2", state: "completed" }]);
    expect(tab(0).log.note).toHaveBeenCalledWith(expect.stringContaining('the download "report.csv" finished'));
    host.dropTasks([1]);
    expect(host.state().downloads).toEqual([]);
  });
});
