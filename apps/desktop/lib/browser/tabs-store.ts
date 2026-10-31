import { useEffect } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import type { BrowserTabSummary } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 内置浏览器标签页的前端镜像。标签页是宿主事实（`electron/main/browser/browser-host.ts`），不进后端
 * snapshot、不进 localStorage：这里启动时读一次，之后只接宿主推送（每次都是全部标签），不轮询。
 * 不在桌面宿主中（浏览器开发、E2E）时始终为空。
 */
type BrowserTabsState = {
  tabs: readonly BrowserTabSummary[];
  replace: (tabs: readonly BrowserTabSummary[]) => void;
};

const useBrowserTabsStore = create<BrowserTabsState>()((set) => ({
  tabs: [],
  replace: (tabs) => set({ tabs }),
}));

let subscribed = false;

/** 整个页面生命周期只订阅一次；推送先注册再读初值，读到的初值不会覆盖更新的推送以外的状态。 */
function ensureSubscription(): void {
  const host = desktopHost;
  if (subscribed || !host) return;
  subscribed = true;
  const { replace } = useBrowserTabsStore.getState();
  let pushed = false;
  host.browser.onTabsChanged((tabs) => {
    pushed = true;
    replace(tabs);
  });
  host.browser.listTabs().then(
    (tabs) => {
      if (!pushed) replace(tabs);
    },
    (error: unknown) => {
      void frontendLog("ERROR", "browser_tabs_read_failed", "读取内置浏览器标签页失败", { error });
    },
  );
}

/** 某个 Task 的标签页，按创建顺序。 */
export function useTaskBrowserTabs(taskId: number): readonly BrowserTabSummary[] {
  useEffect(ensureSubscription, []);
  return useBrowserTabsStore(useShallow((state) => state.tabs.filter((tab) => tab.taskId === taskId)));
}

/** 单个标签页；已关闭时为 undefined。 */
export function useBrowserTab(tabId: string): BrowserTabSummary | undefined {
  useEffect(ensureSubscription, []);
  return useBrowserTabsStore((state) => state.tabs.find((tab) => tab.tabId === tabId));
}
