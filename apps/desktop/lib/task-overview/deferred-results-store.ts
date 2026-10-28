import { create } from "zustand";

import type { DeferredResultEntry } from "@/lib/assistant/contract";

/**
 * 每个 Task 的后台工作看板条目：snapshot `deferred_results` 的只读镜像。
 *
 * 由 Task 的 Assistant 会话在每次提交 state 时写入；离开 Task 页时由 TaskPage 置回 null（与终端会话
 * 镜像同一时机，理由相同：回来时不按过期列表渲染）。看板与页头角标在 Assistant runtime 之外，所以放在
 * 按 taskId 分桶的全局 store。它不是事实源，前端不修改条目：取消、转后台只发命令，等 snapshot 回推。
 */
type DeferredResultsStore = {
  tasks: Record<number, DeferredResultEntry[] | null>;
  sync: (taskId: number, entries: DeferredResultEntry[] | null) => void;
  /** Task（或其所在工作区）删除成功后丢弃该 Task 的桶。 */
  forget: (taskIds: readonly number[]) => void;
};

export const useDeferredResultsStore = create<DeferredResultsStore>()((set) => ({
  tasks: {},
  sync: (taskId, entries) => set((state) => {
    if (state.tasks[taskId] === entries) return state;
    if (entries === null && !(taskId in state.tasks)) return state;
    return { tasks: { ...state.tasks, [taskId]: entries } };
  }),
  forget: (taskIds) => set((state) => {
    if (!taskIds.some((taskId) => taskId in state.tasks)) return state;
    const tasks = { ...state.tasks };
    for (const taskId of taskIds) delete tasks[taskId];
    return { tasks };
  }),
}));

const NO_ENTRIES: readonly DeferredResultEntry[] = [];

/** 某 Task 的看板条目；尚未收到 snapshot 时为空列表。 */
export function useDeferredResults(taskId: number | null): readonly DeferredResultEntry[] {
  return useDeferredResultsStore((state) => (taskId === null ? null : state.tasks[taskId]) ?? NO_ENTRIES);
}

/** 运行中的条目数（页头角标）。 */
export function countRunning(entries: readonly DeferredResultEntry[]): number {
  return entries.reduce((count, entry) => count + (entry.status === "running" ? 1 : 0), 0);
}
