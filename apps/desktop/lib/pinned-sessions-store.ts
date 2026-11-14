import { create } from "zustand";
import { persist } from "zustand/middleware";

import { preferenceStorage } from "@/lib/storage/preference-storage";

type PinnedSessions = { ids: readonly number[] };

type PinnedSessionsState = PinnedSessions & {
  /** 置顶或取消置顶一个会话；置顶总是移到最前。 */
  setPinned: (sessionId: number, pinned: boolean) => void;
  /** Session 删除后移出它们。 */
  forget: (sessionIds: Iterable<number>) => void;
};

/** 读入时收敛：只留去重后的正整数 id，最近置顶的在前；结构不符整体当作没有置顶。 */
export function normalizePinnedSessions(raw: unknown): PinnedSessions {
  const ids = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>).ids : undefined;
  return { ids: Array.isArray(ids) ? [...new Set(ids.filter((id): id is number => Number.isInteger(id) && id > 0))] : [] };
}

/**
 * 侧栏置顶的会话：用户偏好，不是 Session 事实，后端不知道置顶。删除会话时由删除动作移出
 * （`use-workspace-actions.ts`），侧栏只按这份列表摆放。
 */
export const usePinnedSessionsStore = create<PinnedSessionsState>()(persist(
  (set) => ({
    ids: [],
    setPinned: (sessionId, pinned) => set((state) => {
      const rest = state.ids.filter((id) => id !== sessionId);
      return { ids: pinned ? [sessionId, ...rest] : rest };
    }),
    forget: (sessionIds) => set((state) => {
      const removed = new Set(sessionIds);
      return { ids: state.ids.filter((id) => !removed.has(id)) };
    }),
  }),
  {
    name: "cosir:pinned-sessions",
    storage: preferenceStorage<PinnedSessions>(),
    partialize: ({ ids }) => ({ ids }),
    merge: (persisted, current) => ({ ...current, ...normalizePinnedSessions(persisted) }),
  },
));
