import { dequal } from "dequal";
import { create } from "zustand";

/**
 * 一个 Task 的状态：两项正交的事实。`executionStatus` 是当前 Run 的状态（没有 Run 时为 null），
 * `awaitingUserCalls` 是在等你作出决定的调用数（事实源是后端的 HITL 等待占位，与 Run 怎么走无关：
 * Run 被停下或已完成时审批仍可能在等你）。
 */
export type TaskStatus = { executionStatus: string | null; awaitingUserCalls: number };

/**
 * 全部 Task 状态的前端镜像，侧栏、命令面板、页头与工作台标签只从这里读。
 *
 * 唯一的写入方是 Task 推送连接（`useTaskFeed`）：整表帧替换、变化帧合并。不在别处写入，
 * 也不从任务目录接口或对话 snapshot 补值——混用会让同一状态有两个来源。`connected` 为 false 时
 * 保留上次的值；目前只有工作台标签据此标出「未同步」，侧栏、命令面板与页头照常显示上次的值。
 */
type TaskStatusState = {
  tasks: Readonly<Record<number, TaskStatus>>;
  connected: boolean;
  replaceAll: (tasks: Record<number, TaskStatus>) => void;
  merge: (tasks: Record<number, TaskStatus>) => void;
  setConnected: (connected: boolean) => void;
};

export const useTaskStatusStore = create<TaskStatusState>()((set) => ({
  tasks: {},
  connected: false,
  // 值相同的帧（周期整表对账、状态没变的变化帧）不换引用：订阅整表的显示方不必白白重渲染。
  replaceAll: (tasks) => set((state) => (dequal(state.tasks, tasks) ? state : { tasks })),
  merge: (changes) => set((state) => {
    const changed = Object.entries(changes).filter(([taskId, status]) => !dequal(state.tasks[Number(taskId)], status));
    return changed.length === 0 ? state : { tasks: { ...state.tasks, ...Object.fromEntries(changed) } };
  }),
  setConnected: (connected) => set((state) => (state.connected === connected ? state : { connected })),
}));

/** 某个 Task 的执行状态；还没收到推送或没有 Run 时为 null。 */
export function useTaskExecutionStatus(taskId: number | null | undefined): string | null {
  return useTaskStatusStore((state) => (taskId == null ? null : state.tasks[taskId]?.executionStatus ?? null));
}

/** 某个 Task 在等你作出决定的调用数；还没收到推送时为 0。 */
export function useTaskAwaitingUserCalls(taskId: number | null | undefined): number {
  return useTaskStatusStore((state) => (taskId == null ? 0 : state.tasks[taskId]?.awaitingUserCalls ?? 0));
}

export function useTaskStatusConnected(): boolean {
  return useTaskStatusStore((state) => state.connected);
}
