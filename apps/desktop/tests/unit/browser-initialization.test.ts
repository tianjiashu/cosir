import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const contents = vi.hoisted(() => ({
  setBackgroundThrottling: vi.fn(), setWindowOpenHandler: vi.fn(), on: vi.fn(), once: vi.fn(), ipc: { on: vi.fn() },
  loadURL: vi.fn(), close: vi.fn(), getURL: () => "", getTitle: () => "",
  isLoading: () => true, isCrashed: () => false, isDestroyed: () => false, getOSProcessId: () => 1,
  debugger: { attach: vi.fn(), detach: vi.fn(), isAttached: vi.fn(), on: vi.fn(), sendCommand: vi.fn() },
}));
vi.mock("electron", () => ({
  WebContentsView: class { webContents = contents; setBounds() {} },
}));
vi.mock("@/electron/main/browser/browser-session", () => ({ browserPageWebPreferences: () => ({}) }));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { BrowserHost } from "@/electron/main/browser/browser-host";

const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
const limits = { limits: { perTask: 1, total: 1 }, downloadDir: "/workspace/.cosir/Download" };

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
  it.each(["load", "Page.enable", "Page.setInterceptFileChooserDialog", "Network.enable", "Emulation.setFocusEmulationEnabled", "Emulation.setDeviceMetricsOverride"])("在 %s 等待中删除 Task，停止后续步骤并释放资源", async (stage) => {
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
    expect(host.state().tabs).toEqual([]);
  });

  it.each(["load", "attach", "Page.enable", "Page.setInterceptFileChooserDialog", "Network.enable", "Emulation.setFocusEmulationEnabled", "Emulation.setDeviceMetricsOverride"])("%s 失败由 Host 关闭一次，不遗留计时器或标签", async (stage) => {
    const error = new Error(`${stage} failed`);
    if (stage === "load") contents.loadURL.mockRejectedValue(error);
    else if (stage === "attach") contents.debugger.attach.mockImplementation(() => { throw error; });
    else contents.debugger.sendCommand.mockImplementation((method) => method === stage ? Promise.reject(error) : Promise.resolve({}));
    const host = new BrowserHost(() => null);
    await expect(host.userOpen(1, limits)).rejects.toBe(error);
    expect(contents.close).toHaveBeenCalledTimes(1);
    expect(contents.debugger.detach).toHaveBeenCalledTimes(stage === "load" || stage === "attach" ? 0 : 1);
    expect(vi.getTimerCount()).toBe(0);
    expect(host.state().tabs).toEqual([]);
  });

  // 目的：后台标签也能被 Agent 点击；潜在缺陷：没挂过窗口的视图收不到合成鼠标事件，而桌面 E2E 因 Playwright
  // 自己开了焦点模拟测不出来，只能在这里守住初始化顺序。
  it("初始化开启网络日志、焦点模拟，并设上默认的设备模拟", async () => {
    const host = new BrowserHost(() => null);
    await host.userOpen(1, limits);
    const methods = contents.debugger.sendCommand.mock.calls.map(([method]) => method);
    expect(methods).toEqual([
      "Page.enable", "Page.setInterceptFileChooserDialog", "Network.enable", "Emulation.setFocusEmulationEnabled",
      "Emulation.setDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled", "Emulation.setEmulatedMedia",
    ]);
    expect(contents.debugger.sendCommand).toHaveBeenCalledWith("Emulation.setDeviceMetricsOverride", expect.objectContaining({ width: 1280, height: 800, scale: 1, mobile: false }));
    host.closeAll();
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
    expect(host.state().tabs).toEqual([]);
  });

  it("初始化完成与导航开始之间取消，不发起网页加载，保留已就绪标签", async () => {
    const host = new BrowserHost(() => null);
    const owner = new AbortController();
    const opening = host.navigate({ taskId: 1, context: limits, newTab: false, target: { url: "https://example.com" } }, owner.signal);
    // 精确构造 ready 完成后、navigate 续执行前的取消，避免依赖真实线程调度概率。
    const entry = (host as unknown as { tabs: Map<string, { ready: Promise<void> }> }).tabs.get("t1")!;
    void entry.ready.then(() => queueMicrotask(() => owner.abort()));
    // 被取消的请求不会应答（`host-link/client.ts`），拒绝原因无关紧要；要紧的是没有发起加载、标签留着。
    await expect(opening).rejects.toThrow();
    expect(contents.loadURL.mock.calls).toEqual([["about:blank"]]);
    expect(contents.close).not.toHaveBeenCalled();
    expect(host.state().tabs).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
    host.closeAll();
  });
});
