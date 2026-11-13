import { useMemo } from "react";
import { create } from "zustand";

import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { isActiveTerminalSession, terminalBadge } from "@/lib/terminal/terminal-sessions";

/**
 * 每个 Session 的终端工作台 UI 状态。
 *
 * - `sessions` 是 snapshot `terminal_sessions` 的只读镜像（由 Session 的 Assistant 会话在每次提交
 *   state 时写入），null 表示本页还没收到 snapshot；它不是事实源，也不被前端修改。离开 Session 页时
 *   由 SessionPage 置回 null（不能挂在 Assistant 会话卸载上：渲染恢复会重挂会话而终端面板不动，清空
 *   会卸掉正在使用的 xterm），再回来时不按过期列表挂载窗格、判定自动新建。
 * - `open` / `activeSessionId` 是纯 UI 偏好：每个 Session 独立记住，切走再回来恢复；不持久化。
 * - `focusToken` 递增即请求把焦点放到当前终端（打开面板、切换标签、从卡片跳转）。
 * - `acknowledgedEnds` 是用户已经看过的已结束会话：面板打开期间结束的、或打开面板时已在列表里的都算
 *   看过；页头角标只为没看过的异常结束提醒，避免同一次退出一直标红。
 * - `autoCreate` 表示「本次打开时若没有活跃会话需新建一个默认终端」：打开时 snapshot 可能尚未
 *   到达，由工作台在会话列表已知后执行并清除；只在打开那一刻置位，会话退出不会触发新建。
 *
 * 页头按钮在 SessionPage 之外，因此状态放在按 sessionId 分桶的全局 store，而不是组件 state。
 */
export type TerminalWorkbenchSessionState = {
  sessions: TerminalSessionEntry[] | null;
  open: boolean;
  activeSessionId: string | null;
  focusToken: number;
  autoCreate: boolean;
  acknowledgedEnds: readonly string[];
};

type TerminalWorkbenchStore = {
  bySession: Record<number, TerminalWorkbenchSessionState>;
  /** 写入 snapshot 镜像；null 只清空镜像（保留面板偏好），对没有桶的 Session（如已 forget）无动作。 */
  syncSessions: (sessionId: number, sessions: TerminalSessionEntry[] | null) => void;
  setOpen: (sessionId: number, open: boolean) => void;
  toggle: (sessionId: number) => void;
  select: (sessionId: number, terminalSessionId: string | null) => void;
  /** 打开面板、选中会话并请求焦点：工具卡片「在终端中打开」用。 */
  reveal: (sessionId: number, terminalSessionId: string) => void;
  consumeAutoCreate: (sessionId: number) => void;
  /** Session（或其所在工作区）删除成功后丢弃该 Session 的桶，避免已删除 Session 的状态常驻。 */
  forget: (sessionIds: readonly number[]) => void;
};

const EMPTY: TerminalWorkbenchSessionState = {
  sessions: null,
  open: false,
  activeSessionId: null,
  focusToken: 0,
  autoCreate: false,
  acknowledgedEnds: [],
};

function endedIds(sessions: readonly TerminalSessionEntry[] | null): readonly string[] {
  return (sessions ?? []).filter((session) => !isActiveTerminalSession(session)).map((session) => session.terminalSessionId);
}

function hasActiveSession(sessions: TerminalSessionEntry[] | null): boolean {
  return Boolean(sessions?.some(isActiveTerminalSession));
}

/**
 * 选中会话不再可显示时（已从 snapshot 消失）退到第一个活跃会话；选中的已结束会话仍在列表里时保留，
 * 让用户继续看最后画面。
 *
 * 已知边界：新建会话后先按 HTTP 返回的 id 选中，若随后到达一帧尚不含该 id 的列表，这里会把选中改回
 * 别的会话，之后新会话出现也不会恢复。当前不会发生：后端在 worker 握手前就登记会话、刷新时按当时的
 * 登记表整表投影，含新会话的帧不晚于 HTTP 返回太多，且登记后的每一帧都带着它（真实后端并发压测
 * 0/30）。若后端改为握手后才登记、或会话列表推送出现明显延迟，需改为「store 只存用户选择、渲染时
 * 派生实际选中」。
 */
function reconcileActive(sessions: TerminalSessionEntry[], activeSessionId: string | null): string | null {
  if (activeSessionId !== null && sessions.some((session) => session.terminalSessionId === activeSessionId)) return activeSessionId;
  return sessions.find(isActiveTerminalSession)?.terminalSessionId ?? null;
}

export const useTerminalWorkbenchStore = create<TerminalWorkbenchStore>((set) => {
  const update = (sessionId: number, patch: (current: TerminalWorkbenchSessionState) => Partial<TerminalWorkbenchSessionState>) => (
    set((state) => {
      const current = state.bySession[sessionId] ?? EMPTY;
      return { bySession: { ...state.bySession, [sessionId]: { ...current, ...patch(current) } } };
    })
  );
  return {
    bySession: {},
    syncSessions: (sessionId, sessions) => {
      if (sessions === null) {
        set((state) => {
          const current = state.bySession[sessionId];
          if (current?.sessions == null) return state;
          return { bySession: { ...state.bySession, [sessionId]: { ...current, sessions: null } } };
        });
        return;
      }
      set((state) => {
        const current = state.bySession[sessionId] ?? EMPTY;
        if (current.sessions === sessions) return state;
        // 面板开着时结束的会话用户看得见，直接算看过；收起时只保留仍在列表里的记录，列表外的无需再记。
        const ended = endedIds(sessions);
        const acknowledgedEnds = current.open ? ended : current.acknowledgedEnds.filter((id) => ended.includes(id));
        return { bySession: { ...state.bySession, [sessionId]: {
          ...current, sessions, activeSessionId: reconcileActive(sessions, current.activeSessionId), acknowledgedEnds,
        } } };
      });
    },
    setOpen: (sessionId, open) => update(sessionId, (current) => ({
      open,
      autoCreate: open && !current.open && !hasActiveSession(current.sessions),
      focusToken: open ? current.focusToken + 1 : current.focusToken,
      acknowledgedEnds: open ? endedIds(current.sessions) : current.acknowledgedEnds,
    })),
    toggle: (sessionId) => update(sessionId, (current) => ({
      open: !current.open,
      autoCreate: !current.open && !hasActiveSession(current.sessions),
      focusToken: current.open ? current.focusToken : current.focusToken + 1,
      acknowledgedEnds: current.open ? current.acknowledgedEnds : endedIds(current.sessions),
    })),
    select: (sessionId, terminalSessionId) => update(sessionId, (current) => ({
      activeSessionId: terminalSessionId,
      focusToken: current.focusToken + 1,
    })),
    reveal: (sessionId, terminalSessionId) => update(sessionId, (current) => ({
      open: true,
      activeSessionId: terminalSessionId,
      autoCreate: false,
      focusToken: current.focusToken + 1,
      acknowledgedEnds: endedIds(current.sessions),
    })),
    consumeAutoCreate: (sessionId) => update(sessionId, () => ({ autoCreate: false })),
    forget: (sessionIds) => set((state) => {
      if (!sessionIds.some((sessionId) => sessionId in state.bySession)) return state;
      const bySession = { ...state.bySession };
      for (const sessionId of sessionIds) delete bySession[sessionId];
      return { bySession };
    }),
  };
});

/** 读取某 Session 的工作台状态；未出现过的 Session 返回稳定的空状态（避免 selector 每次新建对象）。 */
export function selectTerminalSession(sessionId: number) {
  return (state: { bySession: Record<number, TerminalWorkbenchSessionState> }) => state.bySession[sessionId] ?? EMPTY;
}

/** 页头角标原料：会话镜像与已看过的结束；角标本身由 `terminalBadge` 派生（返回新对象，不放进 selector）。 */
export function useTerminalBadge(sessionId: number | null) {
  const sessions = useTerminalWorkbenchStore((state) => (sessionId === null ? null : state.bySession[sessionId]?.sessions ?? null));
  const acknowledged = useTerminalWorkbenchStore((state) => (sessionId === null ? EMPTY.acknowledgedEnds : state.bySession[sessionId]?.acknowledgedEnds ?? EMPTY.acknowledgedEnds));
  return useMemo(() => terminalBadge(sessions, acknowledged), [acknowledged, sessions]);
}
