import { create } from "zustand";

import type { HandoffEntry } from "@/lib/assistant/contract";
import { revealHandoff } from "@/lib/handoff/reveal";

/**
 * 每个 Task 进行中的交接：snapshot `handoffs` 的只读镜像（事实源是后端交接登记表），终端面板、浏览器面板
 * 的交接提示条都从这里读，按 `surface` + `resourceId` 找到自己的资源。
 *
 * 写入时机与终端会话、后台看板镜像相同：Assistant 会话每次提交 state 时写入，离开 Task 页时置回 null。
 * 新出现的交接（含回到这个 Task 时已在进行的）会把对应面板弹出并选中那个资源——交接是需要用户动手的唯一
 * 时机，弹出的判断只在这一处。前端不修改条目：交还只发决定命令，等 snapshot 回推。
 */
type HandoffStore = {
  tasks: Record<number, readonly HandoffEntry[] | null>;
  sync: (taskId: number, entries: readonly HandoffEntry[] | null) => void;
  /** Task（或其所在工作区）删除成功后丢弃该 Task 的桶。 */
  forget: (taskIds: readonly number[]) => void;
};

export const useHandoffStore = create<HandoffStore>()((set, get) => ({
  tasks: {},
  sync: (taskId, entries) => {
    const previous = get().tasks[taskId] ?? null;
    if (previous === entries) return;
    if (entries === null && !(taskId in get().tasks)) return;
    set((state) => ({ tasks: { ...state.tasks, [taskId]: entries } }));
    if (entries === null) return;
    const known = new Set((previous ?? []).map((entry) => entry.toolCallId));
    for (const entry of entries) {
      if (!known.has(entry.toolCallId)) revealHandoff(taskId, entry);
    }
  },
  forget: (taskIds) => set((state) => {
    if (!taskIds.some((taskId) => taskId in state.tasks)) return state;
    const tasks = { ...state.tasks };
    for (const taskId of taskIds) delete tasks[taskId];
    return { tasks };
  }),
}));

/** 一组交接里落在某个资源上的那条；没有时为 null。 */
export function handoffOn(entries: readonly HandoffEntry[] | null | undefined, surface: HandoffEntry["surface"], resourceId: string): HandoffEntry | null {
  return entries?.find((entry) => entry.surface === surface && entry.resourceId === resourceId) ?? null;
}

/** 某个资源上进行中的交接；没有时为 null。 */
export function useHandoffOn(taskId: number | null, surface: HandoffEntry["surface"], resourceId: string | null | undefined): HandoffEntry | null {
  return useHandoffStore((state) => (taskId === null || !resourceId ? null : handoffOn(state.tasks[taskId], surface, resourceId)));
}
