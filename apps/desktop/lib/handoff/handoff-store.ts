import { create } from "zustand";

import type { HandoffEntry } from "@/lib/assistant/contract";
import { revealBrowserTab } from "@/lib/browser/panel-store";

/**
 * 每个 Session 进行中的交接：snapshot `handoffs` 的只读镜像（事实源是后端交接登记表），浏览器面板
 * 的交接提示条与标签标记都从这里读，按标签 id（`handoffOn`）找到自己的那条。
 *
 * 写入时机与终端会话、后台看板镜像相同：Assistant 会话每次提交 state 时写入，离开 Session 页时置回 null。
 * 新出现的交接（含回到这个 Session 时已在进行的）会把浏览器面板弹出并选中那个标签——交接是需要用户动手的
 * 唯一时机，弹出的判断只在这一处。前端不修改条目：交还只发决定命令，等 snapshot 回推。
 */
type HandoffStore = {
  sessions: Record<number, readonly HandoffEntry[] | null>;
  sync: (sessionId: number, entries: readonly HandoffEntry[] | null) => void;
  /** Session（或其所在工作区）删除成功后丢弃该 Session 的桶。 */
  forget: (sessionIds: readonly number[]) => void;
};

export const useHandoffStore = create<HandoffStore>()((set, get) => ({
  sessions: {},
  sync: (sessionId, entries) => {
    const previous = get().sessions[sessionId] ?? null;
    if (previous === entries) return;
    if (entries === null && !(sessionId in get().sessions)) return;
    set((state) => ({ sessions: { ...state.sessions, [sessionId]: entries } }));
    if (entries === null) return;
    const known = new Set((previous ?? []).map((entry) => entry.toolCallId));
    for (const entry of entries) {
      if (!known.has(entry.toolCallId)) revealBrowserTab(sessionId, entry.resourceId);
    }
  },
  forget: (sessionIds) => set((state) => {
    if (!sessionIds.some((sessionId) => sessionId in state.sessions)) return state;
    const sessions = { ...state.sessions };
    for (const sessionId of sessionIds) delete sessions[sessionId];
    return { sessions };
  }),
}));

/** 一组交接里落在某个浏览器标签上的那条；没有时为 null。 */
export function handoffOn(entries: readonly HandoffEntry[] | null | undefined, tabId: string): HandoffEntry | null {
  return entries?.find((entry) => entry.resourceId === tabId) ?? null;
}
