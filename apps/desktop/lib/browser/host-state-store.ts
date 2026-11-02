import { useEffect } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import type { BrowserDownload, BrowserState, BrowserTabSummary } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 内置浏览器状态（标签页与下载）的前端只读镜像。两者都是宿主事实（`electron/main/browser/browser-host.ts`），
 * 不进后端 snapshot、不进 localStorage：这里启动时读一次，之后只接宿主推送（每次都是完整状态），不轮询。
 * 不在桌面宿主中（浏览器开发、E2E）时始终为空。
 */
const EMPTY_STATE: BrowserState = { tabs: [], downloads: [] };

type BrowserHostStateStore = {
  state: BrowserState;
  replace: (state: BrowserState) => void;
};

const useBrowserHostStateStore = create<BrowserHostStateStore>()((set) => ({
  state: EMPTY_STATE,
  replace: (state) => set({ state }),
}));

let subscribed = false;

/** 整个页面生命周期只订阅一次；推送先注册再读初值，已收到推送时丢弃（更旧的）初值。 */
function ensureSubscription(): void {
  const host = desktopHost;
  if (subscribed || !host) return;
  subscribed = true;
  const { replace } = useBrowserHostStateStore.getState();
  let pushed = false;
  host.browser.onStateChanged((state) => {
    pushed = true;
    replace(state);
  });
  host.browser.state().then(
    (state) => {
      if (!pushed) replace(state);
    },
    (error: unknown) => {
      void frontendLog("ERROR", "browser_state_read_failed", "读取内置浏览器状态失败", { error });
    },
  );
}

/**
 * 订阅标签列表的每次替换（宿主推送或初值），带上替换前的列表，供需要比较前后变化的界面状态（面板选中）使用。
 * 只观察镜像、不触发宿主订阅：镜像有人用时才会开始接推送。
 */
export function onBrowserTabsChange(
  listener: (tabs: readonly BrowserTabSummary[], previous: readonly BrowserTabSummary[]) => void,
): () => void {
  return useBrowserHostStateStore.subscribe((current, previous) => {
    if (current.state.tabs !== previous.state.tabs) listener(current.state.tabs, previous.state.tabs);
  });
}

/** 某个 Task 的标签页，按创建顺序；没有 Task（还没打开对话）时为空。 */
export function useTaskBrowserTabs(taskId: number | null): readonly BrowserTabSummary[] {
  useEffect(ensureSubscription, []);
  return useBrowserHostStateStore(useShallow((store) => store.state.tabs.filter((tab) => tab.taskId === taskId)));
}

/** 单个标签页；已关闭时为 undefined。 */
export function useBrowserTab(tabId: string): BrowserTabSummary | undefined {
  useEffect(ensureSubscription, []);
  return useBrowserHostStateStore((store) => store.state.tabs.find((tab) => tab.tabId === tabId));
}

/** 某个 Task 最近的下载，旧的在前；下载归 Task，触发它的标签关掉后仍在。没有 Task 时为空。 */
export function useTaskBrowserDownloads(taskId: number | null): readonly BrowserDownload[] {
  useEffect(ensureSubscription, []);
  return useBrowserHostStateStore(useShallow((store) => store.state.downloads.filter((download) => download.taskId === taskId)));
}
