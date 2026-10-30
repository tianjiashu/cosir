import { create } from "zustand";

/**
 * 全部 Task 的执行状态（后端 `execution_status`：Task 当前 Run 的状态）的前端镜像，侧栏、命令面板、
 * 页头与工作台标签只从这里读。
 *
 * 唯一的写入方是状态推送连接（`useTaskStatusStream`）：整表帧替换、变化帧合并。不在别处写入，
 * 也不从任务目录接口的 `execution_status` 补值——那是一份按需读取的旧值，混用会让同一状态有两个
 * 来源。`connected` 为 false 时保留上次的值，由显示方标出「未同步」。
 */
type TaskStatusState = {
  statuses: Readonly<Record<number, string | null>>;
  connected: boolean;
  replaceAll: (statuses: Record<number, string | null>) => void;
  merge: (statuses: Record<number, string | null>) => void;
  setConnected: (connected: boolean) => void;
};

export const useTaskStatusStore = create<TaskStatusState>()((set) => ({
  statuses: {},
  connected: false,
  replaceAll: (statuses) => set({ statuses }),
  merge: (changes) => set((state) => ({ statuses: { ...state.statuses, ...changes } })),
  setConnected: (connected) => set((state) => (state.connected === connected ? state : { connected })),
}));

/** 某个 Task 的执行状态；还没收到推送或没有 Run 时为 null。 */
export function useTaskExecutionStatus(taskId: number | null | undefined): string | null {
  return useTaskStatusStore((state) => (taskId == null ? null : state.statuses[taskId] ?? null));
}

export function useTaskStatusConnected(): boolean {
  return useTaskStatusStore((state) => state.connected);
}
