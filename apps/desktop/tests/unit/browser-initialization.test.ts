import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const contents = vi.hoisted(() => ({
  setBackgroundThrottling: vi.fn(), setWindowOpenHandler: vi.fn(), on: vi.fn(),
  loadURL: vi.fn(), close: vi.fn(), getURL: () => "", getTitle: () => "",
  isLoading: () => true, isCrashed: () => false,
  debugger: { attach: vi.fn(), detach: vi.fn(), isAttached: vi.fn(), on: vi.fn(), sendCommand: vi.fn() },
}));
vi.mock("electron", () => ({
  WebContentsView: class { webContents = contents; setBounds() {} },
}));
vi.mock("@/electron/main/browser/browser-session", () => ({ BROWSER_PARTITION: "test" }));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { BrowserHost } from "@/electron/main/browser/browser-host";

const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
const limits = { perTask: 1, total: 1 };

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  contents.loadURL.mockResolvedValue(undefined);
  contents.debugger.sendCommand.mockResolvedValue({});
  let attached = false;
  contents.debugger.attach.mockImplementation(() => { attached = true; });
  contents.debugger.detach.mockImplementation(() => { attached = false; });
  contents.debugger.isAttached.mockImplementation(() => attached);
});
afterEach(() => vi.useRealTimers());

describe("BrowserTab 初始化与 Host 清理", () => {
  it.each(["load", "Page.enable", "Page.setInterceptFileChooserDialog"])("在 %s 等待中删除 Task，停止后续步骤并释放资源", async (stage) => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    if (stage === "load") contents.loadURL.mockReturnValue(pending);
    else contents.debugger.sendCommand.mockImplementation((method) => method === stage ? pending : Promise.resolve({}));
    const host = new BrowserHost(() => null);
    const opening = host.userOpen(1, limits);
    const rejected = expect(opening).rejects.toMatchObject({ code: "TAB_NOT_FOUND" });
    await flush();
    if (stage !== "load") expect(vi.getTimerCount()).toBe(1);
    host.dropTasks([1]);
    await rejected;
    const callsAtClose = contents.debugger.sendCommand.mock.calls.length;
    expect(contents.close).toHaveBeenCalledTimes(1);
    expect(contents.debugger.detach).toHaveBeenCalledTimes(stage === "load" ? 0 : 1);
    expect(vi.getTimerCount()).toBe(0);
    finish();
    await flush();
    expect(contents.debugger.sendCommand).toHaveBeenCalledTimes(callsAtClose);
    expect(host.list()).toEqual([]);
  });

  it.each(["load", "attach", "Page.enable", "Page.setInterceptFileChooserDialog"])("%s 失败由 Host 关闭一次，不遗留计时器或标签", async (stage) => {
    const error = new Error(`${stage} failed`);
    if (stage === "load") contents.loadURL.mockRejectedValue(error);
    else if (stage === "attach") contents.debugger.attach.mockImplementation(() => { throw error; });
    else contents.debugger.sendCommand.mockImplementation((method) => method === stage ? Promise.reject(error) : Promise.resolve({}));
    const host = new BrowserHost(() => null);
    await expect(host.userOpen(1, limits)).rejects.toBe(error);
    expect(contents.close).toHaveBeenCalledTimes(1);
    expect(contents.debugger.detach).toHaveBeenCalledTimes(stage === "load" || stage === "attach" ? 0 : 1);
    expect(vi.getTimerCount()).toBe(0);
    expect(host.list()).toEqual([]);
  });

  it("CDP 初始化超时后关闭资源，迟到拒绝不会成为未处理异常", async () => {
    let fail!: (error: Error) => void;
    contents.debugger.sendCommand.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
    const host = new BrowserHost(() => null);
    const rejected = expect(host.userOpen(1, limits)).rejects.toMatchObject({ code: "PAGE_UNRESPONSIVE" });
    await vi.advanceTimersByTimeAsync(10_000);
    await rejected;
    fail(new Error("late CDP rejection"));
    await flush();
    expect(contents.close).toHaveBeenCalledTimes(1);
    expect(contents.debugger.detach).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    expect(host.list()).toEqual([]);
  });

  it("初始化完成与导航开始之间取消，不发起网页加载，保留已就绪标签", async () => {
    const host = new BrowserHost(() => null);
    const owner = new AbortController();
    const opening = host.navigate({ taskId: 1, limits, newTab: false, target: { url: "https://example.com" } }, owner.signal);
    // 精确构造 ready 完成后、navigate 续执行前的取消，避免依赖真实线程调度概率。
    const entry = (host as unknown as { tabs: Map<string, { ready: Promise<void> }> }).tabs.get("t1")!;
    void entry.ready.then(() => queueMicrotask(() => owner.abort()));
    await expect(opening).rejects.toMatchObject({ code: "CANCELLED" });
    expect(contents.loadURL.mock.calls).toEqual([["about:blank"]]);
    expect(contents.close).not.toHaveBeenCalled();
    expect(host.list()).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
    host.closeAll();
  });
});
