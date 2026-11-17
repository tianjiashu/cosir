import { create } from "zustand";

import type { TransportDeliverable } from "@/lib/assistant/contract";

/**
 * 每个 Session 的会话交付清单：snapshot 顶层 `deliverables` 的只读镜像（后端从完整历史投影，按 key 去重、最近在前）。
 * 侧栏「产出」卡在 Assistant runtime 之外读它，所以放在按 sessionId 分桶的全局 store，写入时机与后台看板相同：
 * Session 的 Assistant 会话每次提交 state 时写入，离开 Session 页时置回 null，删除后丢弃。不是事实源，前端不改条目。
 */
type DeliverablesStore = {
  sessions: Record<number, readonly TransportDeliverable[] | null>;
  sync: (sessionId: number, deliverables: readonly TransportDeliverable[] | null) => void;
  forget: (sessionIds: readonly number[]) => void;
};

export const useDeliverablesStore = create<DeliverablesStore>()((set) => ({
  sessions: {},
  sync: (sessionId, deliverables) => set((state) => {
    if (state.sessions[sessionId] === deliverables) return state;
    if (deliverables === null && !(sessionId in state.sessions)) return state;
    return { sessions: { ...state.sessions, [sessionId]: deliverables } };
  }),
  forget: (sessionIds) => set((state) => {
    if (!sessionIds.some((sessionId) => sessionId in state.sessions)) return state;
    const sessions = { ...state.sessions };
    for (const sessionId of sessionIds) delete sessions[sessionId];
    return { sessions };
  }),
}));

const NONE: readonly TransportDeliverable[] = [];

/** 某 Session 的交付清单；尚未收到 snapshot 时为空列表。 */
export function useSessionDeliverables(sessionId: number | null): readonly TransportDeliverable[] {
  return useDeliverablesStore((state) => (sessionId === null ? null : state.sessions[sessionId]) ?? NONE);
}
