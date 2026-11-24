import { dequal } from "dequal";
import { create } from "zustand";

/**
 * 一个 Session 的状态：几项正交的事实，都由后端推送。`executionStatus` 是当前 Run 的状态（没有 Run 时为 null）；
 * 在等你的调用分两类计数：`awaitingDecisions` 是等你回答的提问（HITL），`awaitingApprovals` 是等你批准的终端命令，
 * 两者都与 Run 怎么走无关（Run 被停下或已完成时仍可能在等你）；`unread` 是最近一次结果（完成或失败）你还没看过，
 * 只有侧栏会话会有。侧栏该怎么提醒由 `session-attention.ts` 一处推导。
 */
export type SessionStatus = {
  executionStatus: string | null;
  awaitingDecisions: number;
  awaitingApprovals: number;
  unread: boolean;
};

/**
 * 全部 Session 状态的前端镜像，侧栏、命令面板与工作台标签只从这里读。
 *
 * 唯一的写入方是 Session 推送连接（`useSessionFeed`）：整表帧替换、变化帧合并。不在别处写入，
 * 也不从会话目录接口或对话 snapshot 补值——混用会让同一状态有两个来源。`connected` 为 false 时
 * 保留上次的值；目前只有工作台标签据此标出「未同步」，侧栏与命令面板照常显示上次的值。
 */
type SessionStatusState = {
  sessions: Readonly<Record<number, SessionStatus>>;
  connected: boolean;
  replaceAll: (sessions: Record<number, SessionStatus>) => void;
  merge: (sessions: Record<number, SessionStatus>) => void;
  setConnected: (connected: boolean) => void;
};

export const useSessionStatusStore = create<SessionStatusState>()((set) => ({
  sessions: {},
  connected: false,
  // 值相同的帧（周期整表对账、状态没变的变化帧）不换引用：订阅整表的显示方不必白白重渲染。
  replaceAll: (sessions) => set((state) => (dequal(state.sessions, sessions) ? state : { sessions })),
  merge: (changes) => set((state) => {
    const changed = Object.entries(changes).filter(([sessionId, status]) => !dequal(state.sessions[Number(sessionId)], status));
    return changed.length === 0 ? state : { sessions: { ...state.sessions, ...Object.fromEntries(changed) } };
  }),
  setConnected: (connected) => set((state) => (state.connected === connected ? state : { connected })),
}));

/** 某个 Session 的全部状态；还没收到推送时为 null。 */
export function useSessionStatus(sessionId: number | null | undefined): SessionStatus | null {
  return useSessionStatusStore((state) => (sessionId == null ? null : state.sessions[sessionId] ?? null));
}

/** 某个 Session 的执行状态；还没收到推送或没有 Run 时为 null。 */
export function useSessionExecutionStatus(sessionId: number | null | undefined): string | null {
  return useSessionStatusStore((state) => (sessionId == null ? null : state.sessions[sessionId]?.executionStatus ?? null));
}

export function useSessionStatusConnected(): boolean {
  return useSessionStatusStore((state) => state.connected);
}
