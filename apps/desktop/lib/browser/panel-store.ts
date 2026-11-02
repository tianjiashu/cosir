import { create } from "zustand";

import type { BrowserTabSummary } from "@/electron/shared/host-contract";
import { useDockStore } from "@/lib/layout/dock-store";
import { onBrowserTabsChange } from "./host-state-store";

type TabRef = Pick<BrowserTabSummary, "tabId" | "taskId" | "openerId">;

/**
 * 浏览器面板正在看哪个 Task 的标签、各 Task 选中的是哪个标签。
 *
 * 默认看当前对话 Task；工具卡片上的「打开」可能来自子 Agent 的对话，所以打开时连同标签所属的 Task 一起
 * 指定。只是界面状态，不持久化。
 *
 * 选中是这里的事实，标签列表是宿主事实（`host-state-store` 镜像）。两者的交汇只有标签列表变化这一刻：本模块订阅
 * 镜像的每次替换，按前后两份列表执行 `followTabChanges`（弹窗开出的标签跟随、关闭后回到来源），不轮询、
 * 也不另记「见过哪些标签」——上一份列表就是比较基准。
 */
type BrowserPanelState = {
  /** 面板显示的 Task；null 表示跟随当前对话。 */
  taskId: number | null;
  selected: Readonly<Record<number, string>>;
  /** 打开某个 Task 的某个标签（工具卡片、`+` 新开后）。 */
  focus: (taskId: number, tabId: string) => void;
  select: (taskId: number, tabId: string) => void;
  /** 回到跟随当前对话（页头按钮打开面板时）。 */
  followConversation: () => void;
};

export const useBrowserPanelStore = create<BrowserPanelState>()((set) => ({
  taskId: null,
  selected: {},
  focus: (taskId, tabId) => set((state) => ({ taskId, selected: { ...state.selected, [taskId]: tabId } })),
  select: (taskId, tabId) => set((state) => ({ selected: { ...state.selected, [taskId]: tabId } })),
  followConversation: () => set({ taskId: null }),
}));

/**
 * 把某个 Task 的某个标签摆到用户面前：面板切到它，并显示浏览器面板。工具卡片的「打开」、交接弹出、链接与新开标签
 * 都经这里，不各自拼这两步。
 */
export function revealBrowserTab(taskId: number, tabId: string): void {
  useBrowserPanelStore.getState().focus(taskId, tabId);
  useDockStore.getState().show("browser");
}

/** 一个 Task 的面板显示哪个标签：选中的那个，没有选中或选中的已不在时回落到最新的标签。 */
export function displayedTab<T extends Pick<BrowserTabSummary, "tabId">>(tabs: readonly T[], selectedId: string | undefined): T | undefined {
  return tabs.find((tab) => tab.tabId === selectedId) ?? tabs.at(-1);
}

/**
 * 标签列表从 `previous` 变成 `next` 后各 Task 的选中，逐 Task 以「变化前面板显示的标签」为准：
 *
 * - 它开出了新标签（新出现、`openerId` 指向它）：切到新标签，同一次出现多个时取最后一个。
 * - 它关闭了：来源标签还在就回到来源；否则清掉选中，由 `displayedTab` 回落到最新的标签。
 *
 * 选中的标签不在旧列表、却在新列表里，说明刚 `focus` 过、镜像这一刻才收到它：以这次显式选择为准，不套规则。
 * 没有变化时原样返回 `selected`，订阅方据此跳过写入。
 */
export function followTabChanges(
  selected: Readonly<Record<number, string>>,
  previous: readonly TabRef[],
  next: readonly TabRef[],
): Readonly<Record<number, string>> {
  let result = selected;
  const update = (taskId: number, tabId: string | undefined) => {
    if (result[taskId] === tabId) return;
    const copy = { ...result };
    if (tabId === undefined) delete copy[taskId];
    else copy[taskId] = tabId;
    result = copy;
  };
  const previousIds = new Set(previous.map((tab) => tab.tabId));
  const nextIds = new Set(next.map((tab) => tab.tabId));
  for (const taskId of new Set(previous.map((tab) => tab.taskId))) {
    const pinned = selected[taskId];
    const shown = displayedTab(previous.filter((tab) => tab.taskId === taskId), pinned);
    if (!shown) continue;
    if (pinned !== undefined && pinned !== shown.tabId && nextIds.has(pinned)) continue;
    const popup = next.findLast((tab) => tab.openerId === shown.tabId && !previousIds.has(tab.tabId));
    if (popup) {
      update(taskId, popup.tabId);
    } else if (!nextIds.has(shown.tabId)) {
      const opener = shown.openerId !== null && nextIds.has(shown.openerId) ? shown.openerId : undefined;
      if (opener !== undefined || pinned === shown.tabId) update(taskId, opener);
    }
  }
  return result;
}

onBrowserTabsChange((next, previous) => {
  const { selected } = useBrowserPanelStore.getState();
  const followed = followTabChanges(selected, previous, next);
  if (followed !== selected) useBrowserPanelStore.setState({ selected: followed });
});
