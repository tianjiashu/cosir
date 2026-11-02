import type { WebContents } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { browserError } from "./errors";

/** 被新导航顶掉或被停止的加载：不算加载失败。 */
export const ERR_ABORTED = -3;

/**
 * 主框架加载完成（含同文档导航）即成功；主框架加载失败、超时或调用被撤销即失败。页面停住（`paused`，JS 弹窗）
 * 也算结束：弹窗挡住了 load 事件（实测：等满时限超时），停着的页面由结果里的弹窗说明。撤销可能来自标签关闭，
 * 这时 webContents 已销毁、不能再 `stop()`（会在监听器里抛出未捕获的异常）：先收尾，活着才停止加载。
 */
export function waitForLoad(contents: WebContents, options: { signal: AbortSignal; paused: AbortSignal; timeoutMs: number; log: Record<string, unknown> }): Promise<void> {
  const { signal, paused, timeoutMs } = options;
  return new Promise((resolve, reject) => {
    const finish = (error?: Error) => {
      clearTimeout(timer);
      contents.off("did-finish-load", onLoad);
      contents.off("did-navigate-in-page", onInPage);
      contents.off("did-fail-load", onFail);
      signal.removeEventListener("abort", onAbort);
      paused.removeEventListener("abort", onLoad);
      if (error) reject(error);
      else resolve();
    };
    const onLoad = () => finish();
    const onInPage = (_event: Electron.Event, _url: string, isMainFrame: boolean) => {
      if (isMainFrame) finish();
    };
    const onFail = (_event: Electron.Event, code: number, description: string, _url: string, isMainFrame: boolean) => {
      if (!isMainFrame || code === ERR_ABORTED) return;
      logDesktopEvent("INFO", "browser", "browser_navigation_failed", { ...options.log, code, description });
      finish(browserError("NAVIGATION_FAILED", `the page failed to load (${description || code})`));
    };
    const stop = () => {
      if (!contents.isDestroyed()) contents.stop();
    };
    const onAbort = () => {
      finish(browserError("CANCELLED", "navigation was cancelled"));
      stop();
    };
    const timer = setTimeout(() => {
      finish(browserError("NAVIGATION_FAILED", `the page did not finish loading within ${timeoutMs / 1000} seconds`));
      stop();
    }, timeoutMs);
    contents.on("did-finish-load", onLoad);
    contents.on("did-navigate-in-page", onInPage);
    contents.on("did-fail-load", onFail);
    signal.addEventListener("abort", onAbort, { once: true });
    paused.addEventListener("abort", onLoad, { once: true });
  });
}

/** 等到页面停止加载、时限到或 `until` 中止（页面停住、调用取消或标签关闭；先到为准，都不算失败）。 */
export function untilStopped(contents: WebContents, timeoutMs: number, until: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      contents.off("did-stop-loading", done);
      until.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, timeoutMs);
    contents.on("did-stop-loading", done);
    if (until.aborted) done();
    else until.addEventListener("abort", done, { once: true });
  });
}
