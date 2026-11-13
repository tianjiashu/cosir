import type { ToolCalls } from "@/lib/assistant/tools/tool-call-index";
import { create } from "zustand";

import type { BackgroundTaskEntry, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";

type BackgroundResults = Pick<TransportState, "background_tasks" | "foreground_waits"> & { toolCalls: ToolCalls };
type ToolParts = Readonly<Record<string, TransportToolCallPart>>;

/**
 * 每个 Session 的后台结果：snapshot `background_tasks`（看板）与 `foreground_waits`（可转后台的前台等待）
 * 的只读镜像，外加看板条目对应的工具 part（`parts`，按 `toolCallId`）：看板展开即渲染对话里的同一张
 * 卡片，两处读同一个 snapshot part，不另存展示数据。
 *
 * 由 Session 的 Assistant 会话在每次提交 state 时写入；离开 Session 页时由 SessionPage 置回 null（与终端会话
 * 镜像同一时机，理由相同：回来时不按过期列表渲染）。看板、页头角标与工具卡片的「转到后台」在 Assistant
 * runtime 之外或跨消息读取，所以放在按 sessionId 分桶的全局 store。它不是事实源，前端不修改条目：取消、
 * 转后台只发命令，等 snapshot 回推。
 */
type BackgroundTasksStore = {
  sessions: Record<number, BackgroundTaskEntry[] | null>;
  foregroundWaits: Record<number, string[] | null>;
  parts: Record<number, ToolParts | null>;
  sync: (sessionId: number, results: BackgroundResults | null) => void;
  /** Session（或其所在工作区）删除成功后丢弃该 Session 的桶。 */
  forget: (sessionIds: readonly number[]) => void;
};

export const useBackgroundTasksStore = create<BackgroundTasksStore>()((set) => ({
  sessions: {},
  foregroundWaits: {},
  parts: {},
  sync: (sessionId, results) => set((state) => {
    const entries = results?.background_tasks ?? null;
    const waits = results?.foreground_waits ?? null;
    const parts = results === null ? null : collectParts(results.toolCalls, results.background_tasks, state.parts[sessionId] ?? null);
    if (state.sessions[sessionId] === entries && state.foregroundWaits[sessionId] === waits && state.parts[sessionId] === parts) return state;
    if (results === null && !(sessionId in state.sessions)) return state;
    return {
      sessions: { ...state.sessions, [sessionId]: entries },
      foregroundWaits: { ...state.foregroundWaits, [sessionId]: waits },
      parts: { ...state.parts, [sessionId]: parts },
    };
  }),
  forget: (sessionIds) => set((state) => {
    if (!sessionIds.some((sessionId) => sessionId in state.sessions)) return state;
    const sessions = { ...state.sessions };
    const foregroundWaits = { ...state.foregroundWaits };
    const parts = { ...state.parts };
    for (const sessionId of sessionIds) {
      delete sessions[sessionId];
      delete foregroundWaits[sessionId];
      delete parts[sessionId];
    }
    return { sessions, foregroundWaits, parts };
  }),
}));

const NO_ENTRIES: readonly BackgroundTaskEntry[] = [];
const NO_WAITS: readonly string[] = [];

/** 某 Session 的看板条目；尚未收到 snapshot 时为空列表。 */
export function useBackgroundTasks(sessionId: number | null): readonly BackgroundTaskEntry[] {
  return useBackgroundTasksStore((state) => (sessionId === null ? null : state.sessions[sessionId]) ?? NO_ENTRIES);
}

/** 某 Session 正在前台等待、可转到后台的调用 id；尚未收到 snapshot 时为空列表。 */
export function useForegroundWaits(sessionId: number | null): readonly string[] {
  return useBackgroundTasksStore((state) => (sessionId === null ? null : state.foregroundWaits[sessionId]) ?? NO_WAITS);
}

/**
 * 看板条目对应的工具 part；尚未收到 snapshot、或那一回合已不在对话里（如被撤回）时为 null，
 * 看板退回单行。
 */
export function useBackgroundTaskPart(sessionId: number, toolCallId: string): TransportToolCallPart | null {
  return useBackgroundTasksStore((state) => state.parts[sessionId]?.[toolCallId] ?? null);
}

/** 仅按看板条目查共享索引；未变 part 沿用上一张表，正文帧不通知看板订阅者。 */
function collectParts(
  toolCalls: ToolCalls,
  entries: readonly BackgroundTaskEntry[],
  previous: ToolParts | null,
): ToolParts | null {
  if (entries.length === 0) return previous === null || Object.keys(previous).length === 0 ? previous : {};
  const found: Record<string, TransportToolCallPart> = {};
  for (const entry of entries) {
    const part = toolCalls.get(entry.toolCallId);
    if (part) found[entry.toolCallId] = part;
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
