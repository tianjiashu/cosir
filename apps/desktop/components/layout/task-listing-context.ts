import { createContext, useContext } from "react";

/**
 * 「这个 Task 应该已经在侧栏里」：Agent 用 `fork_task` 分出的对话在父 Run 仍在运行时就建好了，而侧栏
 * 目录只在标题变化或一轮输出结束时刷新，分叉卡片出现时据此补刷一次。由 `AppLayout` 用工作区目录提供
 * （`useWorkspaceCatalog().ensureTaskListed`）；没有提供方（工作台阅读器之外的只读场景）时为 null。
 */
export const TaskListingContext = createContext<((taskId: number) => void) | null>(null);

export function useEnsureTaskListed(): ((taskId: number) => void) | null {
  return useContext(TaskListingContext);
}
