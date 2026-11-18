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

const REQUEST_POLL_MS = 100;

/** 落定的结局：哪一层收尾，以及给 Agent 说明用的证据。`aborted`：页面停住或调用取消，调用方不再说明。 */
export type Settling<T> = { waitedMs: number; reads: number } & (
  | { by: "stable" | "unreadable" | "aborted" }
  | { by: "requests"; pending: T[] }
  | { by: "changing"; changed: number; example: string | null }
);

/**
 * 动作之后等页面落定，分两层判定，到 `timeoutMs` 为止：
 * - 内容请求（`pending`）：还有可能带回内容的请求在路上就继续等，回来之后重新判稳定。只看页面会被骨架屏骗过：
 *   慢接口期间页面静止好几秒（实测搜索接口 4.7 秒）。
 * - 页面稳定（`read` 读出 Agent 将看到的大纲）：相隔 `intervalMs` 的两次读取完全相同才算好。只看网络会被两头骗过：
 *   持续的埋点让它永远等不到安静，走 WebSocket 推送或没有请求的分批渲染它又看不见。
 *
 * 已知局限：页面先静止超过 `intervalMs`、之后才由推送送来内容的，两层都分不出来，只能靠到上限后的说明与
 * Agent 的 `wait_for`。`read` 返回 null（读不了：弹窗挡住、页面崩溃）就不再等，由页面结果去说明。
 */
export async function untilPageSettled<T, O>(
  probe: { pending: () => T[]; read: () => Promise<O | null>; changed: (previous: O, current: O) => { count: number; example: string | null } },
  options: { intervalMs: number; timeoutMs: number; until: AbortSignal },
): Promise<Settling<T>> {
  const start = Date.now();
  let previous: O | null = null;
  let reads = 0;
  let lastChange: { count: number; example: string | null } = { count: 0, example: null };
  const done = () => ({ waitedMs: Date.now() - start, reads });
  for (;;) {
    if (options.until.aborted) return { ...done(), by: "aborted" };
    const pending = probe.pending();
    if (pending.length > 0) {
      // 内容还在路上：它回来之后页面会变，之前读到的稳定与变化都不作数（变化的证据跟着比对基准一起作废，
      // 否则请求回来后只来得及读一次就到上限时，会拿请求前的变化报「还在变」）。
      previous = null;
      lastChange = { count: 0, example: null };
      if (Date.now() - start >= options.timeoutMs) return { ...done(), by: "requests", pending };
      await pause(REQUEST_POLL_MS, options.until);
      continue;
    }
    const current = await probe.read();
    reads += 1;
    if (current === null) return { ...done(), by: "unreadable" };
    if (previous !== null) {
      lastChange = probe.changed(previous, current);
      if (lastChange.count === 0) return { ...done(), by: "stable" };
    }
    previous = current;
    if (Date.now() - start >= options.timeoutMs) {
      // 到上限时只读到一次、没看到过变化：没有「还在变」的证据，按好了交出。
      return lastChange.count > 0 ? { ...done(), by: "changing", changed: lastChange.count, example: lastChange.example } : { ...done(), by: "stable" };
    }
    await pause(options.intervalMs, options.until);
  }
}

/** 暂停 `ms`；`until` 中止时提前结束（由循环开头统一处理）。 */
function pause(ms: number, until: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); until.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, ms);
    until.addEventListener("abort", finish, { once: true });
  });
}

/** 导航真正停止的旁路观察；不随请求取消/弹窗提前结束，用于修改操作交出槽位。 */
export function navigationCompletion(contents: WebContents): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      contents.off("did-stop-loading", done);
      contents.off("destroyed", done);
      contents.off("did-navigate-in-page", samePage);
      resolve();
    };
    const samePage = (_event: Electron.Event, _url: string, isMainFrame: boolean) => { if (isMainFrame && !contents.isLoading()) done(); };
    contents.once("did-stop-loading", done);
    contents.once("destroyed", done);
    contents.on("did-navigate-in-page", samePage);
  });
}
