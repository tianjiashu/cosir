import { create } from "zustand";

import type { BackgroundTaskEntry, TransportState } from "@/lib/assistant/contract";

type DeferredResults = Pick<TransportState, "background_tasks" | "foreground_waits">;

/**
 * 每个 Task 的延迟结果：snapshot `background_tasks`（看板）与 `foreground_waits`（可转后台的前台等待）
 * 的只读镜像。
 *
 * 由 Task 的 Assistant 会话在每次提交 state 时写入；离开 Task 页时由 TaskPage 置回 null（与终端会话
 * 镜像同一时机，理由相同：回来时不按过期列表渲染）。看板、页头角标与工具卡片的「转到后台」在 Assistant
 * runtime 之外或跨消息读取，所以放在按 taskId 分桶的全局 store。它不是事实源，前端不修改条目：取消、
 * 转后台只发命令，等 snapshot 回推。
 */
type BackgroundTasksStore = {
  tasks: Record<number, BackgroundTaskEntry[] | null>;
  foregroundWaits: Record<number, string[] | null>;
  sync: (taskId: number, results: DeferredResults | null) => void;
  /** Task（或其所在工作区）删除成功后丢弃该 Task 的桶。 */
  forget: (taskIds: readonly number[]) => void;
};

export const useBackgroundTasksStore = create<BackgroundTasksStore>()((set) => ({
  tasks: {},
  foregroundWaits: {},
  sync: (taskId, results) => set((state) => {
    const entries = results?.background_tasks ?? null;
    const waits = results?.foreground_waits ?? null;
    if (state.tasks[taskId] === entries && state.foregroundWaits[taskId] === waits) return state;
    if (results === null && !(taskId in state.tasks)) return state;
    return {
      tasks: { ...state.tasks, [taskId]: entries },
      foregroundWaits: { ...state.foregroundWaits, [taskId]: waits },
    };
  }),
  forget: (taskIds) => set((state) => {
    if (!taskIds.some((taskId) => taskId in state.tasks)) return state;
    const tasks = { ...state.tasks };
    const foregroundWaits = { ...state.foregroundWaits };
    for (const taskId of taskIds) {
      delete tasks[taskId];
      delete foregroundWaits[taskId];
    }
    return { tasks, foregroundWaits };
  }),
}));

const NO_ENTRIES: readonly BackgroundTaskEntry[] = [];
const NO_WAITS: readonly string[] = [];

/** 某 Task 的看板条目；尚未收到 snapshot 时为空列表。 */
export function useBackgroundTasks(taskId: number | null): readonly BackgroundTaskEntry[] {
  return useBackgroundTasksStore((state) => (taskId === null ? null : state.tasks[taskId]) ?? NO_ENTRIES);
}

/** 某 Task 正在前台等待、可转到后台的调用 id；尚未收到 snapshot 时为空列表。 */
export function useForegroundWaits(taskId: number | null): readonly string[] {
  return useBackgroundTasksStore((state) => (taskId === null ? null : state.foregroundWaits[taskId]) ?? NO_WAITS);
}

/** 运行中的条目数（页头角标）。 */
export function countRunning(entries: readonly BackgroundTaskEntry[]): number {
  return entries.reduce((count, entry) => count + (entry.status === "running" ? 1 : 0), 0);
}
