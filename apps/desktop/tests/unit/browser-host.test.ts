import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TabEvents, TabOptions } from "@/electron/main/browser/browser-tab";
import { waitForInitialization } from "@/electron/main/browser/initialization";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/browser/browser-tab", () => ({
  get BrowserTab() { return FakeTab; }, VIEWPORT: { width: 1280, height: 800 },
}));
vi.mock("@/electron/main/browser/browser-session", () => ({ browserPageWebPreferences: () => ({}) }));
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
  note = vi.fn();
  navigate = vi.fn().mockResolvedValue(undefined);
  snapshot = vi.fn().mockResolvedValue({ text: "page", url: "", title: "", loading: false, frames: 0, notes: [], dialog: null });
  readonly contents = { isDestroyed: () => false };
  dialog: { id: number; type: "confirm"; message: string; defaultText: string } | null = null;
  processId = 1;
  readonly pausedSignal = new AbortController().signal;
  interruptCdp = vi.fn();
  readonly tabId: string; readonly taskId: number; readonly events: TabEvents; readonly options: TabOptions;
  get openerId() { return this.options.openerId; }
  get downloadDir() { return this.options.downloadDir; }
  get createdBy() { return this.options.createdBy; }
  constructor(options: TabOptions) {
    this.options = options;
    this.tabId = options.tabId; this.taskId = options.taskId; this.events = options.events;
    FakeTab.instances.push(this);
  }
  initialize(signal: AbortSignal) { return waitForInitialization(this.pending, signal); }
  summary() {
    return { tabId: this.tabId, taskId: this.taskId, createdBy: this.createdBy, openerId: this.openerId, url: "", title: "", loading: true, crashed: false, dialog: null };
  }
}

const context = (perTask = 2, total = 3) => ({ limits: { perTask, total }, downloadDir: "/workspace/.cosir/Download" });
const limits = context();
const tab = (index = 0) => FakeTab.instances[index];
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
beforeEach(() => { FakeTab.instances = []; });

describe("BrowserHost 资源生命周期", () => {
  it("初始化中即可列出标签；快照等待初始化，且不跨 Task 访问", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits })).toMatchObject([{ tab_id: "t1", current: true }]);
    const snapshot = host.snapshot({ taskId: 1, fromLine: 1, context: limits });
    await expect(host.snapshot({ taskId: 2, tab: "t1", fromLine: 1, context: limits })).rejects.toMatchObject({ code: "TAB_NOT_FOUND" });
    expect(tab().snapshot).not.toHaveBeenCalled();
    tab().finish();
    await expect(opening).resolves.toBe("t1");
    await expect(snapshot).resolves.toMatchObject({ tab_id: "t1", snapshot: "page" });
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
    const waiting = host.snapshot({ taskId: 1, fromLine: 1, context: limits }, waiter.signal);
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
    expect(tab().note).toHaveBeenCalledWith(expect.stringContaining("in a new tab t2"));
    tab(1).finish(); await flush();
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits }).find((item) => item.current)?.tab_id).toBe("t1");
    tab(1).events.onGone();
    expect(host.state().tabs.map((item: { tabId: string }) => item.tabId)).toEqual(["t1"]);
    expect(tab(1).close).toHaveBeenCalledTimes(1);
    expect(tab().note).toHaveBeenLastCalledWith("tab t2 (opened by this page) closed itself");
  });

  it("非网页地址、超出上限与宿主退出后都拒绝开窗", async () => {
    const host = new BrowserHost(() => null);
    const first = host.userOpen(1, context(1, 5));
    tab().finish(); await first;
    expect(tab().events.onWindowOpen("file:///etc/passwd")).toEqual({ action: "deny" });
    expect(tab().events.onWindowOpen("https://example.com")).toEqual({ action: "deny" });
    expect(tab().note).toHaveBeenLastCalledWith(expect.stringContaining("per-task limit"));
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
    expect(host.manageTabs({ taskId: 1, action: "list", context: limits }).find((item) => item.current)?.tab_id).toBe("t2");
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
    expect(tab(0).note).toHaveBeenCalledWith(expect.stringContaining('the download "report.csv" finished'));
    host.dropTasks([1]);
    expect(host.state().downloads).toEqual([]);
  });
});
