import { useEffect } from "react";
import { create } from "zustand";

import type { BrowserTabSummary } from "@/electron/shared/host-contract";
import { useLingering } from "@/hooks/use-lingering";
import { onBrowserTabsChange, useDisplayedBrowserTabs, useSessionBrowserTabs } from "./host-state-store";

/**
 * 页头浏览器入口的两段式提醒：Agent 正在用浏览器时「呼吸」，停手后若还有你没看过的 Agent 标签就「静止」，
 * 浏览器面板正在显示这个会话时不提醒这个会话自己的标签。形状与终端的 `terminalBadge` 对齐。
 *
 * 「正在用」有两个来源，提示分开说：这个会话的 Agent，与它委派出的子 Agent（标签的 `displaySessionId` 是这个会话、
 * 所属 Session 不是）。子 Agent 的标签不在面板里，所以面板开着时也提醒；它们不计入标签数，也不算「没看过」——
 * 面板看不到，记了就永远消不掉。两者都读宿主的 `agentBusy`（此刻在执行），各自由 `useLingering` 在两次调用之间撑住，
 * 一个停手不会让另一个的提示跳出来。「没看过」是前端自己的
 * 界面状态：镜像里新出现的 Agent 标签记为没看过（首份镜像不算，见 `onBrowserTabsChange`），面板显示哪个会话就把
 * 哪个会话的全部记为看过（`useMarkShownSessionSeen`，不论它是不是当前对话：卡片上的「打开」会把面板指定到子
 * Agent 的会话）、标签关闭即移除。都不进宿主、后端或 localStorage。
 */
export type BrowserBadge = {
  count: number;
  tone: "browsing" | "unseen" | null;
  /** 提醒的一句话说明（拼进入口的 tooltip 与无障碍标签）；无提醒为 null。 */
  hint: string | null;
};

/** Agent 停手后「正在用」再保持多久：盖住相邻两次工具调用之间的空档。 */
const BROWSING_LINGER_MS = 2000;

type UnseenStore = {
  /** 每个 Session 还没看过的 Agent 标签 id，按出现顺序。 */
  bySession: Readonly<Record<number, readonly string[]>>;
  markSeen: (sessionId: number) => void;
};

export const useUnseenAgentTabsStore = create<UnseenStore>()((set) => ({
  bySession: {},
  markSeen: (sessionId) => set((state) => {
    if (!state.bySession[sessionId]?.length) return state;
    const bySession = { ...state.bySession };
    delete bySession[sessionId];
    return { bySession };
  }),
}));

/**
 * 标签列表从 `previous` 换成 `next` 后的「没看过」：加上新出现的 Agent 标签，去掉已关闭的。
 * `previous` 为 null（首份镜像）时只做去除，不加；没有变化时原样返回，订阅方据此跳过写入。
 */
export function followUnseenTabs(
  unseen: Readonly<Record<number, readonly string[]>>,
  previous: readonly BrowserTabSummary[] | null,
  next: readonly BrowserTabSummary[],
): Readonly<Record<number, readonly string[]>> {
  const open = new Set(next.map((tab) => tab.tabId));
  const known = new Set((previous ?? []).map((tab) => tab.tabId));
  const result: Record<number, readonly string[]> = {};
  let changed = false;
  for (const [key, ids] of Object.entries(unseen)) {
    const kept = ids.filter((id) => open.has(id));
    if (kept.length !== ids.length) changed = true;
    if (kept.length > 0) result[Number(key)] = kept;
  }
  if (previous !== null) {
    for (const tab of next) {
      if (tab.createdBy !== "agent" || known.has(tab.tabId)) continue;
      result[tab.sessionId] = [...(result[tab.sessionId] ?? []), tab.tabId];
      changed = true;
    }
  }
  return changed ? result : unseen;
}

/**
 * 纯规则：`showing` 是浏览器面板此刻正显示这个会话，那时不提醒这个会话自己的标签（正在用、没看过）；
 * `delegatedBrowsing`（子 Agent 正在用）面板里看不到，照常提醒。这个会话自己的 Agent 在用时只说它。
 */
export function browserBadge({ tabs, unseen, browsing, delegatedBrowsing, showing }: {
  tabs: readonly BrowserTabSummary[];
  unseen: readonly string[];
  browsing: boolean;
  delegatedBrowsing: boolean;
  showing: boolean;
}): BrowserBadge {
  const count = tabs.length;
  if (browsing && !showing) return { count, tone: "browsing", hint: "Agent 正在使用浏览器" };
  if (delegatedBrowsing) return { count, tone: "browsing", hint: "子 Agent 正在使用浏览器" };
  if (unseen.length > 0 && !showing) return { count, tone: "unseen", hint: `Agent 打开了 ${unseen.length} 个标签页` };
  return { count, tone: null, hint: null };
}

const EMPTY: readonly string[] = [];

/** 当前会话的页头浏览器提醒；只读，「看过」由 `useMarkShownSessionSeen` 按面板实际显示的会话记。 */
export function useBrowserBadge(sessionId: number | null, showing: boolean): BrowserBadge {
  const tabs = useSessionBrowserTabs(sessionId);
  const displayed = useDisplayedBrowserTabs(sessionId);
  const unseen = useUnseenAgentTabsStore((state) => (sessionId === null ? EMPTY : state.bySession[sessionId] ?? EMPTY));
  const browsing = useLingering(tabs.some((tab) => tab.agentBusy), BROWSING_LINGER_MS, sessionId);
  const delegatedBrowsing = useLingering(
    displayed.some((tab) => tab.sessionId !== sessionId && tab.agentBusy),
    BROWSING_LINGER_MS,
    sessionId,
  );
  return browserBadge({ tabs, unseen, browsing, delegatedBrowsing, showing });
}

/** 浏览器面板正在显示 `sessionId`（不显示为 null）：它的 Agent 标签全部记为看过，显示期间新出现的也随即记上。 */
export function useMarkShownSessionSeen(sessionId: number | null): void {
  const hasUnseen = useUnseenAgentTabsStore((state) => sessionId !== null && (state.bySession[sessionId]?.length ?? 0) > 0);
  useEffect(() => {
    if (hasUnseen && sessionId !== null) useUnseenAgentTabsStore.getState().markSeen(sessionId);
  }, [hasUnseen, sessionId]);
}

onBrowserTabsChange((next, previous) => {
  const { bySession } = useUnseenAgentTabsStore.getState();
  const followed = followUnseenTabs(bySession, previous, next);
  if (followed !== bySession) useUnseenAgentTabsStore.setState({ bySession: followed });
});
