import type { WebContents } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { browserError } from "./errors";

/** 被新导航顶掉或被停止的加载：不算加载失败。 */
export const ERR_ABORTED = -3;

/**
 * 等一次主框架导航落定，返回是否打开了新页面。结束信号只有一个：页面不再处于加载状态（`did-stop-loading`，与
 * `untilStopped` 同一个判断）；期间发生的事只用来分类结局，不比谁先到：
 * - 换上了文档（`did-navigate`）→ 打开了；
 * - 主框架真正的加载失败（不是被新导航顶掉的 `ERR_ABORTED`）→ 立即以 `NAVIGATION_FAILED` 结束；
 * - 都没有 → 没有打开新页面：转成了下载、服务器没有返回内容（204）、跳转被拦下时，浏览器放弃这次导航、页面
 *   留在原地，既不发完成也不发失败（实测），只有加载状态结束。
 *
 * 被新导航顶掉（重定向、加载中脚本改 `location`）时加载状态一直持续，不会中途结束。同文档跳转（改锚点、
 * `pushState`）不一定经过加载状态，没有跨文档导航开始时当场结束；新页面加载中脚本改地址栏不算结束。页面停住
 * （`paused`，JS 弹窗）也算结束：弹窗挡住了加载（实测：等满时限超时），停着的页面由结果里的弹窗说明，不判断
 * 有没有打开。撤销可能来自标签关闭，这时 webContents 已销毁、不能再 `stop()`（会在监听器里抛出未捕获的异常）：
 * 先收尾，活着才停止加载。
 */
export function waitForLoad(contents: WebContents, options: { signal: AbortSignal; paused: AbortSignal; timeoutMs: number; log: Record<string, unknown> }): Promise<boolean> {
  const { signal, paused, timeoutMs } = options;
  let crossDocument = false;
  let committed = false;
  return new Promise((resolve, reject) => {
    const settle = (outcome: boolean | Error) => {
      clearTimeout(timer);
      contents.off("did-start-navigation", onStart);
      contents.off("did-navigate", onCommit);
      contents.off("did-navigate-in-page", onInPage);
      contents.off("did-fail-load", onFail);
      contents.off("did-stop-loading", onStop);
      signal.removeEventListener("abort", onAbort);
      paused.removeEventListener("abort", onPaused);
      if (outcome instanceof Error) reject(outcome);
      else resolve(outcome);
    };
    const onStart = (details: Electron.Event<{ isMainFrame: boolean; isSameDocument: boolean }>) => {
      if (details.isMainFrame && !details.isSameDocument) crossDocument = true;
    };
    const onCommit = () => {
      committed = true;
    };
    const onInPage = (_event: Electron.Event, _url: string, isMainFrame: boolean) => {
      if (isMainFrame && !crossDocument) settle(true);
    };
    const onFail = (_event: Electron.Event, code: number, description: string, _url: string, isMainFrame: boolean) => {
      if (!isMainFrame || code === ERR_ABORTED) return;
      logDesktopEvent("INFO", "browser", "browser_navigation_failed", { ...options.log, code, description });
      settle(browserError("NAVIGATION_FAILED", `the page failed to load (${description || code})`));
    };
    const onStop = () => {
      if (!committed) logDesktopEvent("INFO", "browser", "browser_navigation_not_opened", options.log);
      settle(committed);
    };
    const onPaused = () => settle(true);
    const stop = () => {
      if (!contents.isDestroyed()) contents.stop();
    };
    const onAbort = () => {
      settle(browserError("CANCELLED", "navigation was cancelled"));
      stop();
    };
    const timer = setTimeout(() => {
      settle(browserError("NAVIGATION_FAILED", `the page did not finish loading within ${timeoutMs / 1000} seconds`));
      stop();
    }, timeoutMs);
    contents.on("did-start-navigation", onStart);
    contents.on("did-navigate", onCommit);
    contents.on("did-navigate-in-page", onInPage);
    contents.on("did-fail-load", onFail);
    contents.on("did-stop-loading", onStop);
    signal.addEventListener("abort", onAbort, { once: true });
    paused.addEventListener("abort", onPaused, { once: true });
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
