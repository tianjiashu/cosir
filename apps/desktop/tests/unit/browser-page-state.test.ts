import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import type { DownloadItem, IpcMainEvent } from "electron";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { BrowserDownloads } from "@/electron/main/browser/downloads";
import { PageDialogs } from "@/electron/main/browser/page-dialogs";
import { waitForLoad } from "@/electron/main/browser/page-loading";
import { DEFAULT_VIEWPORT, FOLLOW_SETTLE_MS, TabEmulation } from "@/electron/main/browser/tab-emulation";

const ipcEvent = () => ({ returnValue: undefined as unknown }) as IpcMainEvent & { returnValue: unknown };

describe("PageDialogs", () => {
  // 目的：作答对准看见的那个弹窗；潜在缺陷：Agent 刚答完、页面紧接着弹新的，用户过时的点击被算到新弹窗上。
  it("answers only the dialog with the given id and returns the page's value", async () => {
    const dialogs = new PageDialogs();
    const first = ipcEvent();
    const opened = dialogs.open(first, { type: "prompt", message: "name?", defaultText: "x" });
    if (!("dialog" in opened)) throw new Error("not opened");
    const settled = dialogs.settled();
    expect(dialogs.answer(opened.dialog.id + 1, { accept: true })).toBeNull();
    expect(first.returnValue).toBeUndefined();
    expect(dialogs.answer(opened.dialog.id, { accept: true })).toMatchObject({ type: "prompt" });
    expect(first.returnValue).toBe("x");
    await settled;

    const second = ipcEvent();
    const next = dialogs.open(second, { type: "confirm", message: "sure?", defaultText: "" });
    expect("dialog" in next && next.dialog.id).toBeGreaterThan(opened.dialog.id);
    expect(dialogs.answer(opened.dialog.id, { accept: true })).toBeNull();
    expect(dialogs.dismiss()).toMatchObject({ type: "confirm" });
    expect(second.returnValue).toBe(false);
  });

  // 目的：同时第二个弹窗不能静默消失；潜在缺陷：iframe 里的弹窗被吞掉，调用方不知道。
  it("refuses a second dialog with cancel and says why", () => {
    const dialogs = new PageDialogs();
    dialogs.open(ipcEvent(), { type: "alert", message: "a", defaultText: "" });
    const extra = ipcEvent();
    expect(dialogs.open(extra, { type: "confirm", message: "b", defaultText: "" })).toEqual({ refused: "busy" });
    expect(extra.returnValue).toBe(false);
    expect(dialogs.open(ipcEvent(), { type: "beforeunload", message: "", defaultText: "" })).toEqual({ refused: "unknown-type" });
  });
});

/** Electron `DownloadItem` 的最小替身：事件由测试手动发出。 */
function fakeItem(filename: string): DownloadItem & EventEmitter & { savePath?: string; cancelled: boolean; state: string } {
  const item = Object.assign(new EventEmitter(), {
    savePath: undefined as string | undefined,
    cancelled: false,
    state: "progressing",
    getFilename: () => filename,
    getTotalBytes: () => 10,
    getReceivedBytes: () => 5,
    getState: () => item.state,
    setSavePath: (target: string) => { item.savePath = target; },
    cancel: () => { item.cancelled = true; item.state = "cancelled"; },
  });
  return item as never;
}

describe("BrowserDownloads", () => {
  const directory = () => fs.mkdtempSync(path.join(os.tmpdir(), "cosir-downloads-"));

  // 目的：同名下载不互相覆盖；潜在缺陷：两个标签同时下载同名文件，完成前磁盘上看不出占用，写到同一路径。
  it("avoids names taken on disk and by other downloads still in progress, across tabs", () => {
    const dir = directory();
    fs.writeFileSync(path.join(dir, "report.csv"), "old");
    const downloads = new BrowserDownloads({ onChange: vi.fn(), note: vi.fn() });
    const a = fakeItem("report.csv");
    const b = fakeItem("report.csv");
    downloads.accept(a, { taskId: 1, tabId: "t1", directory: dir });
    downloads.accept(b, { taskId: 2, tabId: "t5", directory: dir });
    expect([a.savePath, b.savePath]).toEqual([path.join(dir, "report (1).csv"), path.join(dir, "report (2).csv")]);
  });

  // 目的：进行中的下载始终受管；潜在缺陷：挤出列表后再也取消不了、也不参与同名避让。
  it("keeps downloads in progress when trimming, and only drops finished ones beyond the limit", () => {
    const dir = directory();
    const downloads = new BrowserDownloads({ onChange: vi.fn(), note: vi.fn() });
    const running = fakeItem("big.zip");
    downloads.accept(running, { taskId: 1, tabId: "t1", directory: dir });
    for (let index = 0; index < 7; index += 1) {
      const item = fakeItem(`f${index}.txt`);
      downloads.accept(item, { taskId: 1, tabId: "t1", directory: dir });
      item.emit("done", {}, "completed");
    }
    const kept = downloads.list();
    expect(kept.map((item) => item.filename)).toEqual(["big.zip", "f2.txt", "f3.txt", "f4.txt", "f5.txt", "f6.txt"]);
    downloads.dropTasks([1]);
    expect(running.cancelled).toBe(true);
    expect(downloads.list()).toEqual([]);
  });

  // 目的：落盘失败在边界收住；潜在缺陷：异常漏出 will-download，Chromium 弹系统另存为框、Agent 一无所知。
  it("cancels and tells the agent when the directory cannot be created", () => {
    const blocker = path.join(directory(), "file");
    fs.writeFileSync(blocker, "");
    const note = vi.fn();
    const downloads = new BrowserDownloads({ onChange: vi.fn(), note });
    const item = fakeItem("a.csv");
    downloads.accept(item, { taskId: 3, tabId: "t2", directory: path.join(blocker, "sub") });
    expect(item.cancelled).toBe(true);
    expect(downloads.list()).toEqual([]);
    expect(note).toHaveBeenCalledWith(3, "t2", expect.stringContaining("could not be saved"));
  });

  it("publishes state changes immediately and reports the result with the read-only hint", () => {
    const onChange = vi.fn();
    const note = vi.fn();
    const downloads = new BrowserDownloads({ onChange, note });
    const item = fakeItem("a.csv");
    downloads.accept(item, { taskId: 1, tabId: "t1", directory: directory() });
    onChange.mockClear();
    item.emit("updated", {}, "interrupted");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(downloads.list()[0]).toMatchObject({ state: "progressing", stalled: true });
    item.emit("done", {}, "completed");
    expect(downloads.list()[0]).toMatchObject({ state: "completed", stalled: false });
    expect(note).toHaveBeenLastCalledWith(1, "t1", expect.stringMatching(/finished: .*read-only \.cosir area/));
  });

  // 目的：断线续传中的下载仍是进行中；潜在缺陷：可恢复的中断被当成结局——被挤出列表后再也取消不了，删 Task 时
  // 既不取消又注销，续传完成后文件写进已删除 Task 的工作区。
  it("keeps a download that is resuming after an interruption under management", () => {
    const dir = directory();
    const downloads = new BrowserDownloads({ onChange: vi.fn(), note: vi.fn() });
    const resuming = fakeItem("big.zip");
    downloads.accept(resuming, { taskId: 1, tabId: "t1", directory: dir });
    resuming.state = "interrupted";
    resuming.emit("updated", {}, "interrupted");
    for (let index = 0; index < 7; index += 1) {
      const item = fakeItem(`f${index}.txt`);
      downloads.accept(item, { taskId: 1, tabId: "t1", directory: dir });
      item.emit("done", {}, "completed");
    }
    expect(downloads.list().map((item) => item.filename)).toContain("big.zip");
    downloads.dropTasks([1]);
    expect(resuming.cancelled).toBe(true);
  });
});

describe("TabEmulation", () => {
  type Send = (method: string, params: Record<string, unknown>, options?: import("@/electron/main/browser/cdp-session").SendOptions) => Promise<unknown>;
  const observed = (send: Send): Send => (method, params, options) => {
    const raw = send(method, params, options);
    options?.onCommand?.(raw);
    return raw;
  };
  const metricsCalls = (send: ReturnType<typeof vi.fn<Send>>) => send.mock.calls.filter(([method]) => method === "Emulation.setDeviceMetricsOverride");
  const metricsWidths = (send: ReturnType<typeof vi.fn<Send>>) => metricsCalls(send).map(([, params]) => params.width);
  const live = () => new AbortController().signal;
  /** 面板上的占位矩形（窗口坐标）。 */
  const box = (width: number, height: number) => ({ x: 0, y: 0, width, height });
  const pinned = { width: 1280, height: 800, mobile: false };
  const settled = () => new Promise((resolve) => setTimeout(resolve, FOLLOW_SETTLE_MS + 50));

  // 目的：默认按面板大小 1:1 显示，用户看得清、交接时点得准；潜在缺陷：固定视口被等比缩小到看不清（实际出现过：
  // 面板约 450pt 宽时缩放 0.35）。
  it("follows the panel at full size and keeps that size when taken off the panel", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(640, 400));
    await emulation.start(live());
    expect(metricsCalls(send).at(-1)).toEqual(["Emulation.setDeviceMetricsOverride", expect.objectContaining({ width: 640, height: 400, scale: 1 }), expect.anything()]);
    expect(emulation.current).toEqual({ width: 640, height: 400, mobile: false, colorScheme: null, follows: true });
    expect(emulation.viewBounds).toEqual(box(640, 400));
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 });

    // 从面板摘下：Agent 手上的页面不重新排版。
    send.mockClear();
    emulation.hide();
    await settled();
    expect(metricsWidths(send)).toEqual([]);
    expect(emulation.viewBounds).toEqual({ x: 0, y: 0, width: 640, height: 400 });
    // 挂回同样大小的面板：仍然不变。
    emulation.show(box(640, 400));
    await settled();
    expect(metricsWidths(send)).toEqual([]);
  });

  // 目的：拖动面板边缘时不逐帧重新排版；潜在缺陷：每一帧都改视口，页面反复重排、Agent 的快照与坐标不断失效。
  it("follows a new panel size only after it settles, showing the old viewport scaled meanwhile", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(640, 400));
    await emulation.start(live());
    send.mockClear();
    emulation.show(box(700, 440));
    emulation.show(box(800, 500));
    emulation.show(box(960, 600));
    // 停稳之前：视口不变，只按比例放大显示。
    await vi.waitFor(() => expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 150, y: 15 }));
    expect(emulation.current.width).toBe(640);
    await settled();
    expect(emulation.current).toMatchObject({ width: 960, height: 600, follows: true });
    expect(new Set(metricsWidths(send))).toEqual(new Set([640, 960]));
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 });
  });

  it("settles an already queued panel resize before starting input", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    await emulation.start(live());
    emulation.show(box(640, 400));
    await emulation.prepareInput(live());
    expect(emulation.current).toMatchObject({ width: 640, height: 400, follows: true });
    expect(emulation.toInput({ x: 100, y: 20 })).toEqual({ x: 100, y: 20 });
    const count = metricsCalls(send).length;
    await settled();
    expect(metricsCalls(send)).toHaveLength(count);
  });

  // 目的：Agent 固定视口后与面板无关，只等比缩放显示；auto 立即回到跟随面板。潜在缺陷：固定的视口随面板变窄
  // 切到手机布局，或改回跟随后要等下一次拖动面板才生效。
  it("keeps a pinned viewport whatever the panel does, and follows again at once when asked", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(640, 400));
    await emulation.start(live());
    await emulation.set({ viewport: pinned }, live());
    expect(emulation.current).toEqual({ ...pinned, colorScheme: null, follows: false });
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 });
    emulation.show(box(320, 400));
    await settled();
    expect(emulation.current.width).toBe(1280);
    await vi.waitFor(() => expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 25, y: 2.5 }));

    await emulation.set({ viewport: "follow" }, live());
    expect(emulation.current).toEqual({ width: 320, height: 400, mobile: false, colorScheme: null, follows: true });
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 });
  });

  // 目的：合成点击按页面上已生效的缩放换算；潜在缺陷：设备模拟没设上（失败、被弹窗闸住、还在途）时按想要的比例换算，点到别处。
  it("converts input with the applied scale and catches up after a failure", async () => {
    let fail = false;
    const send = vi.fn<Send>(async () => { if (fail) throw new Error("paused"); });
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(640, 400));
    expect(send).not.toHaveBeenCalled();
    await emulation.start(live());
    await emulation.set({ viewport: pinned }, live());
    expect(metricsCalls(send).at(-1)).toEqual(["Emulation.setDeviceMetricsOverride", expect.objectContaining({ width: 1280, height: 800, scale: 0.5 }), expect.anything()]);
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 });

    fail = true;
    const before = metricsCalls(send).length;
    emulation.show(box(1280, 800));
    await vi.waitFor(() => expect(metricsCalls(send)).toHaveLength(before + 1));
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 });
    // 失败后停下等外部触发：不原地重试（被弹窗闸住时命令立即失败，重试只会空转）。
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(metricsCalls(send)).toHaveLength(before + 1);

    fail = false;
    emulation.resync();
    await vi.waitFor(() => expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 }));
  });

  // 目的：Agent 改视口后再导航，页面按新视口布局；潜在缺陷：设置还在途就开始加载，或深浅色与视口互相覆盖。
  it("waits for an agent's change to take effect, sends only what changed, and moves the view with it", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const metricsApplied = vi.fn();
    const emulation = new TabEmulation(observed(send), vi.fn(), metricsApplied);
    await emulation.start(live());
    send.mockClear();
    await emulation.set({ colorScheme: "dark" }, live());
    expect(send.mock.calls.map(([method]) => method)).toEqual(["Emulation.setEmulatedMedia"]);
    expect(send).toHaveBeenCalledWith("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] }, expect.anything());

    send.mockClear();
    metricsApplied.mockClear();
    await emulation.set({ viewport: { width: 390, height: 844, mobile: true } }, live());
    expect(send.mock.calls.map(([method]) => method)).toEqual(["Emulation.setDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled"]);
    expect(emulation.current).toEqual({ width: 390, height: 844, mobile: true, colorScheme: "dark", follows: false });
    // 后台视图与视口同大；视口生效时通知（视图 bounds 跟着它）。
    expect(emulation.viewBounds).toEqual({ x: 0, y: 0, width: 390, height: 844 });
    expect(metricsApplied).toHaveBeenCalledWith(expect.objectContaining({ width: 390, height: 844, scale: 1 }));

    // 挂到面板上：在占位矩形里等比、水平居中、顶部对齐。
    emulation.show({ x: 100, y: 40, width: 600, height: 422 });
    expect(emulation.viewBounds).toEqual({ x: 303, y: 40, width: 195, height: 422 });
    await vi.waitFor(() => expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 }));
  });

  // 目的：挂在面板上时改视口，一条命令就设对缩放；潜在缺陷：缩放按旧摆放宽度除以新视口算，先错一帧再补一轮。
  it("sets the right scale in one command when the viewport changes while shown", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(600, 700));
    await emulation.start(live());
    send.mockClear();
    await emulation.set({ viewport: { width: 390, height: 844, mobile: false } }, live());
    const scale = Math.min(600 / 390, 700 / 844);
    expect(metricsCalls(send)).toEqual([["Emulation.setDeviceMetricsOverride", expect.objectContaining({ width: 390, height: 844, scale }), expect.anything()]]);
    expect(emulation.viewBounds).toEqual({ x: Math.round((600 - 390 * scale) / 2), y: 0, width: Math.round(390 * scale), height: 700 });
  });

  // 目的：失败的要求不残留；潜在缺陷：结果报着没生效的视口，或等哪次弹窗答完被悄悄补设。
  it("reports a failed agent change, keeps reporting what is in effect, and drops the request", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    await emulation.start(live());
    const before = emulation.current;
    expect(before).toEqual({ ...DEFAULT_VIEWPORT, mobile: false, colorScheme: null, follows: true });
    const paused = new Error("paused");
    send.mockRejectedValue(paused);
    await expect(emulation.set({ viewport: { width: 800, height: 600, mobile: false } }, live())).rejects.toBe(paused);
    expect(emulation.current).toEqual(before);
    expect(emulation.viewBounds).toEqual({ x: 0, y: 0, ...DEFAULT_VIEWPORT });

    send.mockResolvedValue(undefined);
    send.mockClear();
    emulation.resync();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(metricsCalls(send)).toHaveLength(0);
  });

  // 目的：Agent 取消调用只结束它自己的等待；潜在缺陷：共用的追赶随第一个调用方的取消停下，面板摆放的缩放卡在旧值。
  it("lets a cancelled agent change go without stalling the shared catch-up", async () => {
    let release!: () => void;
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    await emulation.start(live());
    await emulation.set({ viewport: pinned }, live());
    send.mockImplementationOnce(() => new Promise<undefined>((resolve) => { release = () => resolve(undefined); }));
    const owner = new AbortController();
    const change = emulation.set({ viewport: { width: 640, height: 400, mobile: false } }, owner.signal);
    emulation.show(box(1280, 800));
    owner.abort(new Error("cancelled"));
    await expect(change).rejects.toThrow("cancelled");
    release();
    // 取消的要求不再追；摆放带来的缩放照常追上（1280 宽视口放进 1280 宽的占位，缩放 1）。
    await vi.waitFor(() => expect(emulation.current).toMatchObject(pinned));
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 });
  });

  // 目的：同一次设置里前一条命令成功、后一条失败时如实记下；潜在缺陷：整次不记导致缩放按旧值换算、点偏，
  // 或之后一次无关的摆放 / 弹窗答复把已生效的部分悄悄撤回。
  it("keeps the commands that took effect when a later one fails, without reverting them later", async () => {
    const send = vi.fn<Send>(async () => undefined);
    const emulation = new TabEmulation(observed(send), vi.fn(), vi.fn());
    emulation.show(box(640, 400));
    await emulation.start(live());
    send.mockImplementation(async (method) => { if (method === "Emulation.setEmulatedMedia") throw new Error("paused"); });
    await expect(emulation.set({ viewport: { width: 320, height: 200, mobile: false }, colorScheme: "dark" }, live())).rejects.toThrow("paused");
    // 视口命令已生效：报告与输入换算都按它（320 宽放进 640 宽的占位，缩放 2）。
    expect(emulation.current).toEqual({ width: 320, height: 200, mobile: false, colorScheme: null, follows: false });
    expect(emulation.toInput({ x: 100, y: 10 })).toEqual({ x: 200, y: 20 });

    send.mockReset();
    send.mockResolvedValue(undefined);
    emulation.resync();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(send).not.toHaveBeenCalled();
  });
});

describe("waitForLoad", () => {
  // 目的：加载中标签被关掉时干净地结束；潜在缺陷：对已销毁的 webContents 调 stop() 在监听器里抛出，主进程弹出
  // 未捕获异常框，等待永不落定、计时器 30 秒后再抛一次。
  it("ends when the tab goes away mid-load without touching the destroyed contents", async () => {
    vi.useFakeTimers();
    try {
      const contents = Object.assign(new EventEmitter(), {
        isDestroyed: () => true,
        stop: vi.fn(() => { throw new Error("Object has been destroyed"); }),
      });
      const owner = new AbortController();
      const loading = waitForLoad(contents as never, { signal: owner.signal, paused: new AbortController().signal, timeoutMs: 30_000, log: {} });
      owner.abort();
      await expect(loading).rejects.toMatchObject({ code: "CANCELLED" });
      expect(contents.stop).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
