import fs from "node:fs";
import path from "node:path";

import type { DownloadItem } from "electron";

import { logDesktopEvent } from "../desktop-log";
import type { BrowserDownload } from "../../shared/host-contract";

/** 每个 Task 留几次已结束的下载（面板下载条与回答「下到哪了」够用）；进行中的从不挤掉。 */
const KEPT_FINISHED_PER_TASK = 5;
/** 进度推送的最小间隔：`updated` 事件很密，每次都推送会刷屏 IPC。状态变化不受它限制。 */
const PROGRESS_PUBLISH_MS = 500;
/** 给 Agent 的提示：下载目录在工作区 `.cosir` 只读区里，文件工具只能读。 */
const READ_ONLY_HINT = "it is in the workspace's read-only .cosir area: read it directly, or copy it with the terminal to change or move it";

type DownloadEvents = {
  onChange: () => void;
  /** 给 Task 的 Agent 一条备注；`tabId` 是触发下载的标签（可能已关闭，由调用方决定落到哪个标签）。 */
  note: (taskId: number, tabId: string, text: string) => void;
};

/**
 * 内置浏览器全部下载的登记：下载归 Task，不归标签（与浏览器下载归 profile 一致）——触发它的标签关掉
 * （弹出页下载后自己 `window.close()` 很常见）不取消下载；只有 Task 删除与宿主退出才取消。
 *
 * 落盘到 Task 所在工作区的下载目录（标签创建时由后端给出），同名按「名字 (n).扩展名」避让，占用判断跨全部
 * 登记（不同标签、不同 Task 可能写同一个目录）与磁盘。状态投影进 `BrowserState.downloads`（面板下载条），
 * 开始与结束各给 Agent 一条带绝对路径的备注。
 */
export class BrowserDownloads {
  private readonly entries: { download: BrowserDownload; item: DownloadItem }[] = [];
  private sequence = 0;
  private lastPublishAt = 0;

  constructor(private readonly events: DownloadEvents) {}

  /**
   * 必须在 `will-download` 里同步调用：保存路径只能在事件处理期间设定，否则 Chromium 会弹保存框。
   * 目录建不出来等外部 IO 失败在这里收住：取消下载并告诉 Agent，不让异常漏出事件处理。
   */
  accept(item: DownloadItem, source: { taskId: number; tabId: string; directory: string }): void {
    let target: string;
    try {
      fs.mkdirSync(source.directory, { recursive: true });
      target = this.freePath(source.directory, path.basename(item.getFilename()) || "download");
      item.setSavePath(target);
    } catch (error) {
      item.cancel();
      const message = error instanceof Error ? error.message : String(error);
      logDesktopEvent("WARNING", "browser", "browser_download_failed", { task_id: source.taskId, tab_id: source.tabId, error: message });
      this.events.note(source.taskId, source.tabId, `a download of "${item.getFilename()}" could not be saved (${message}) and was cancelled`);
      return;
    }
    const download: BrowserDownload = {
      id: `d${++this.sequence}`,
      taskId: source.taskId,
      tabId: source.tabId,
      filename: path.basename(target),
      path: target,
      state: "progressing",
      stalled: false,
      receivedBytes: 0,
      totalBytes: item.getTotalBytes(),
    };
    this.entries.push({ download, item });
    this.trim(source.taskId);
    // `updated` 里的 interrupted 是可恢复的中断（Chromium 会续传），不是结局：结局只在 `done` 里写（见 `ended`）。
    item.on("updated", (_event, state) => {
      const stalled = state === "interrupted";
      const changed = stalled !== download.stalled;
      download.stalled = stalled;
      download.receivedBytes = item.getReceivedBytes();
      download.totalBytes = item.getTotalBytes();
      if (changed || Date.now() - this.lastPublishAt >= PROGRESS_PUBLISH_MS) this.publish();
    });
    item.once("done", (_event, state) => {
      download.state = state;
      download.stalled = false;
      download.receivedBytes = item.getReceivedBytes();
      download.totalBytes = item.getTotalBytes();
      this.trim(download.taskId);
      this.publish();
      logDesktopEvent("INFO", "browser", "browser_download_done", { task_id: download.taskId, download_id: download.id, state });
      this.events.note(download.taskId, download.tabId, state === "completed"
        ? `the download "${download.filename}" finished: ${target} (${READ_ONLY_HINT})`
        : `the download "${download.filename}" ${state === "cancelled" ? "was cancelled" : "failed"}`);
    });
    this.publish();
    logDesktopEvent("INFO", "browser", "browser_download_started", { task_id: download.taskId, tab_id: download.tabId, download_id: download.id });
    this.events.note(download.taskId, download.tabId, `a download started: "${download.filename}" is being saved to ${target}`);
  }

  list(): BrowserDownload[] {
    return this.entries.map(({ download }) => ({ ...download }));
  }

  pathOf(downloadId: string): string | undefined {
    return this.entries.find(({ download }) => download.id === downloadId)?.download.path;
  }

  /** Task 删除：取消它未完成的下载并忘掉它的全部记录。 */
  dropTasks(taskIds: readonly number[]): void {
    this.remove((download) => taskIds.includes(download.taskId));
  }

  /** 宿主退出：取消全部未完成的下载。 */
  cancelAll(): void {
    this.remove(() => true);
  }

  private remove(matches: (download: BrowserDownload) => boolean): void {
    const removed = this.entries.filter(({ download }) => matches(download));
    if (removed.length === 0) return;
    for (const { download, item } of removed) {
      if (!ended(download)) item.cancel();
    }
    this.entries.splice(0, this.entries.length, ...this.entries.filter((entry) => !removed.includes(entry)));
    this.publish();
  }

  /** 每个 Task 只留最近几次已结束的；进行中的不挤掉（挤掉就再也取消不了、也不参与同名避让）。 */
  private trim(taskId: number): void {
    const finished = this.entries.filter(({ download }) => download.taskId === taskId && ended(download));
    for (const entry of finished.slice(0, Math.max(0, finished.length - KEPT_FINISHED_PER_TASK))) {
      this.entries.splice(this.entries.indexOf(entry), 1);
    }
  }

  private publish(): void {
    this.lastPublishAt = Date.now();
    this.events.onChange();
  }

  /** 同名避让：磁盘上已有的与登记里正在写的都算占用（Chromium 下载完成前写的是临时文件名，磁盘上看不出）。 */
  private freePath(directory: string, filename: string): string {
    const taken = new Set(this.entries.map(({ download }) => download.path));
    const { name, ext } = path.parse(filename);
    for (let index = 0; ; index += 1) {
      const candidate = path.join(directory, index === 0 ? filename : `${name} (${index})${ext}`);
      if (!taken.has(candidate) && !fs.existsSync(candidate)) return candidate;
    }
  }
}

/**
 * 下载是否已结束：只看 `done` 写下的结局。挤出列表（`trim`）、删除时要不要取消（`remove`）都按它判断——正在续传的
 * 下载不算结束，挤掉就再也取消不了，删 Task 时不取消它，续传完成后会写进已删除 Task 的工作区。
 */
function ended(download: BrowserDownload): boolean {
  return download.state !== "progressing";
}
