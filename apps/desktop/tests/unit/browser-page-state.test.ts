import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import type { DownloadItem, IpcMainEvent } from "electron";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { BrowserDownloads } from "@/electron/main/browser/downloads";
import { PageDialogs } from "@/electron/main/browser/page-dialogs";
import { TabViewport } from "@/electron/main/browser/tab-viewport";

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
    expect(downloads.list()[0].state).toBe("interrupted");
    item.emit("done", {}, "completed");
    expect(note).toHaveBeenLastCalledWith(1, "t1", expect.stringMatching(/finished: .*read-only \.cosir area/));
  });
});

describe("TabViewport", () => {
  // 目的：合成点击按页面上已生效的缩放换算；潜在缺陷：设备模拟没设上（失败、被弹窗闸住、还在途）时按想要的比例换算，点到别处。
  it("converts input with the applied scale and catches up after a failure", async () => {
    let fail = false;
    const apply = vi.fn(async () => { if (fail) throw new Error("paused"); });
    const viewport = new TabViewport(apply, vi.fn());
    viewport.show(640);
    expect(apply).not.toHaveBeenCalled();
    await viewport.start(new AbortController().signal);
    expect(apply).toHaveBeenLastCalledWith(expect.objectContaining({ width: 1280, height: 800, scale: 0.5 }), expect.anything());
    expect(viewport.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 });

    fail = true;
    viewport.show(1280);
    await vi.waitFor(() => expect(apply).toHaveBeenCalledTimes(2));
    expect(viewport.toInput({ x: 100, y: 10 })).toEqual({ x: 50, y: 5 });

    fail = false;
    viewport.resync();
    await vi.waitFor(() => expect(viewport.toInput({ x: 100, y: 10 })).toEqual({ x: 100, y: 10 }));
    expect(viewport.isShown).toBe(true);
    viewport.hide();
    expect(viewport.isShown).toBe(false);
  });
});
