import { create } from "zustand";

import type { BackgroundTaskEntry, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";

type DeferredResults = Pick<TransportState, "background_tasks" | "foreground_waits" | "messages">;
type ToolParts = Readonly<Record<string, TransportToolCallPart>>;

/**
 * 每个 Task 的延迟结果：snapshot `background_tasks`（看板）与 `foreground_waits`（可转后台的前台等待）
 * 的只读镜像，外加看板条目对应的工具 part（`parts`，按 `toolCallId`）：看板展开即渲染对话里的同一张
 * 卡片，两处读同一个 snapshot part，不另存展示数据。
 *
 * 由 Task 的 Assistant 会话在每次提交 state 时写入；离开 Task 页时由 TaskPage 置回 null（与终端会话
 * 镜像同一时机，理由相同：回来时不按过期列表渲染）。看板、页头角标与工具卡片的「转到后台」在 Assistant
 * runtime 之外或跨消息读取，所以放在按 taskId 分桶的全局 store。它不是事实源，前端不修改条目：取消、
 * 转后台只发命令，等 snapshot 回推。
 */
type BackgroundTasksStore = {
  tasks: Record<number, BackgroundTaskEntry[] | null>;
  foregroundWaits: Record<number, string[] | null>;
  parts: Record<number, ToolParts | null>;
  sync: (taskId: number, results: DeferredResults | null) => void;
  /** Task（或其所在工作区）删除成功后丢弃该 Task 的桶。 */
  forget: (taskIds: readonly number[]) => void;
};

export const useBackgroundTasksStore = create<BackgroundTasksStore>()((set) => ({
  tasks: {},
  foregroundWaits: {},
  parts: {},
  sync: (taskId, results) => set((state) => {
    const entries = results?.background_tasks ?? null;
    const waits = results?.foreground_waits ?? null;
    const parts = results === null ? null : collectParts(results.messages, results.background_tasks, state.parts[taskId] ?? null);
    if (state.tasks[taskId] === entries && state.foregroundWaits[taskId] === waits && state.parts[taskId] === parts) return state;
    if (results === null && !(taskId in state.tasks)) return state;
    return {
      tasks: { ...state.tasks, [taskId]: entries },
      foregroundWaits: { ...state.foregroundWaits, [taskId]: waits },
      parts: { ...state.parts, [taskId]: parts },
    };
  }),
  forget: (taskIds) => set((state) => {
    if (!taskIds.some((taskId) => taskId in state.tasks)) return state;
    const tasks = { ...state.tasks };
    const foregroundWaits = { ...state.foregroundWaits };
    const parts = { ...state.parts };
    for (const taskId of taskIds) {
      delete tasks[taskId];
      delete foregroundWaits[taskId];
      delete parts[taskId];
    }
    return { tasks, foregroundWaits, parts };
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

/**
 * 看板条目对应的工具 part；尚未收到 snapshot、或那一轮已不在对话里（如编辑重跑移出）时为 null，
 * 看板退回单行。
 */
export function useBackgroundTaskPart(taskId: number, toolCallId: string): TransportToolCallPart | null {
  return useBackgroundTasksStore((state) => state.parts[taskId]?.[toolCallId] ?? null);
}

/**
 * 按条目 id 从消息里取工具 part。每次提交 state 都会调用（流式输出时很频繁），所以只在有条目时扫描，
 * 且各 part 与上次同一引用时沿用上次的表：snapshot mutation 按路径结构共享，未变的 part 引用不变，
 * 订阅方因此不会在无关帧上重渲染。
 */
function collectParts(
  messages: readonly TransportState["messages"][number][],
  entries: readonly BackgroundTaskEntry[],
  previous: ToolParts | null,
): ToolParts | null {
  if (entries.length === 0) return previous === null || Object.keys(previous).length === 0 ? previous : {};
  const wanted = new Set(entries.map((entry) => entry.toolCallId));
  const found: Record<string, TransportToolCallPart> = {};
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type === "tool-call" && wanted.has(part.toolCallId)) found[part.toolCallId] = part;
    }
  }
  const keys = Object.keys(found);
  if (previous !== null && keys.length === Object.keys(previous).length && keys.every((key) => previous[key] === found[key])) {
    return previous;
  }
  return found;
}

/** 运行中的条目数（页头角标）。 */
export function countRunning(entries: readonly BackgroundTaskEntry[]): number {
  return entries.reduce((count, entry) => count + (entry.status === "running" ? 1 : 0), 0);
}
