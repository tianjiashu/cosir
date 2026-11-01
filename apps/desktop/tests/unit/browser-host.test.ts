import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserTabCreator } from "@/electron/shared/host-contract";
import type { TabEvents } from "@/electron/main/browser/browser-tab";
import { waitForInitialization } from "@/electron/main/browser/initialization";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/browser/browser-tab", () => ({
  get BrowserTab() { return FakeTab; }, DETACHED_VIEWPORT: { x: 0, y: 0, width: 1280, height: 800 },
}));
import { BrowserHost } from "@/electron/main/browser/browser-host";

class FakeTab {
  static instances: FakeTab[] = [];
  finish!: () => void;
  fail!: (error: Error) => void;
  pending = new Promise<void>((resolve, reject) => { this.finish = resolve; this.fail = reject; });
  close = vi.fn();
  note = vi.fn();
  navigate = vi.fn().mockResolvedValue(undefined);
  snapshot = vi.fn().mockResolvedValue({ text: "page", url: "", title: "", loading: false, frames: 0, notes: [] });
  constructor(readonly tabId: string, readonly taskId: number, readonly createdBy: BrowserTabCreator, readonly events: TabEvents) {
    FakeTab.instances.push(this);
  }
  initialize(signal: AbortSignal) { return waitForInitialization(this.pending, signal); }
  summary() { return { tabId: this.tabId, taskId: this.taskId, createdBy: this.createdBy, url: "", title: "", loading: true, crashed: false }; }
}

const limits = { perTask: 2, total: 3 };
const tab = (index = 0) => FakeTab.instances[index];
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
beforeEach(() => { FakeTab.instances = []; });

describe("BrowserHost 资源生命周期", () => {
  it("初始化中即可列出标签；快照等待初始化，且不跨 Task 访问", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    expect(host.manageTabs({ taskId: 1, action: "list" })).toMatchObject([{ tab_id: "t1", current: true }]);
    const snapshot = host.snapshot({ taskId: 1, fromLine: 1 });
    await expect(host.snapshot({ taskId: 2, tab: "t1", fromLine: 1 })).rejects.toMatchObject({ code: "TAB_NOT_FOUND" });
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
    expect(host.list()).toEqual([]);
    expect(tab().close).toHaveBeenCalledTimes(1);
    if (kind === "shutdown") await expect(host.userOpen(1, limits)).rejects.toMatchObject({ code: "UNAVAILABLE" });
  });

  it("初始化失败回收视图和名额，后续标签不复用 id", async () => {
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, { perTask: 1, total: 1 });
    const rejected = expect(opening).rejects.toThrow("initialization failed");
    tab().fail(new Error("initialization failed"));
    await rejected;
    expect(tab().close).toHaveBeenCalledTimes(1);
    expect(host.list()).toEqual([]);
    const next = host.userOpen(1, { perTask: 1, total: 1 });
    tab(1).finish();
    await expect(next).resolves.toBe("t2");
  });

  it("新建请求取消回收资源；已有标签的等待者取消不关闭共享标签", async () => {
    const host = new BrowserHost(() => null);
    const owner = new AbortController();
    const opening = host.navigate({ taskId: 1, limits, newTab: true, target: { url: "https://example.com" } }, owner.signal);
    const rejected = expect(opening).rejects.toMatchObject({ name: "AbortError" });
    owner.abort();
    await rejected;
    expect(tab().close).toHaveBeenCalledTimes(1);

    const next = host.userOpen(1, limits);
    const waiter = new AbortController();
    const waiting = host.snapshot({ taskId: 1, fromLine: 1 }, waiter.signal);
    const cancelled = expect(waiting).rejects.toMatchObject({ name: "AbortError" });
    waiter.abort();
    await cancelled;
    expect(tab(1).close).not.toHaveBeenCalled();
    tab(1).finish();
    await next;
  });

  it("popup 不覆盖期间的新选择；关闭当前标签回落到最新标签", async () => {
    const host = new BrowserHost(() => null);
    const first = host.userOpen(1, { perTask: 5, total: 5 });
    tab().finish(); await first;
    tab().events.onPopup("https://example.com");
    const third = host.userOpen(1, { perTask: 5, total: 5 });
    tab(2).finish(); await third;
    tab(1).finish(); await flush();
    expect(host.manageTabs({ taskId: 1, action: "list" }).find((item) => item.current)?.tab_id).toBe("t3");
    host.userClose("t3");
    expect(host.manageTabs({ taskId: 1, action: "list" }).find((item) => item.current)?.tab_id).toBe("t2");
    host.userClose("t1");
    tab().events.onPopup("https://late.example.com");
    expect(FakeTab.instances).toHaveLength(3);
  });
});
