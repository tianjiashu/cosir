import { useMemo } from "react";
import { create } from "zustand";

import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { isActiveTerminalSession, newlyPrefilled, terminalBadge } from "@/lib/terminal/terminal-sessions";

/**
 * 每个 Session 的终端工作台 UI 状态。
 *
 * - `sessions` 是 snapshot `terminal_sessions` 的只读镜像（由 Session 的 Assistant 会话在每次提交
 *   state 时写入），null 表示本页还没收到 snapshot；它不是事实源，也不被前端修改。离开 Session 页时
 *   由 SessionPage 置回 null（不能挂在 Assistant 会话卸载上：渲染恢复会重挂会话而终端面板不动，清空
 *   会卸掉正在使用的 xterm），再回来时不按过期列表挂载窗格、判定自动新建。
 * - `open` / `activeSessionId` 是纯 UI 偏好：每个 Session 独立记住，切走再回来恢复；不持久化。
 * - `focusPending` 是一次待执行的焦点请求：只有用户操作（打开面板、切换标签、从卡片跳转、关掉标签后换到相邻的）
 *   置位，由可见的那个窗格执行一次后经 `consumeFocus` 清除；收起面板即作废。焦点只来自请求、不随「变得可见」
 *   顺带给：Agent 填入命令也会让面板出现，那时用户可能正在输入框打字。请求放在 store 而不是窗格里记「处理过
 *   哪一次」：Agent 新开的终端窗格挂载时也要能分辨请求是不是早已执行过。
 * - `acknowledgedEnds` 是用户已经看过的已结束会话：面板打开期间结束的、或打开面板时已在列表里的都算
 *   看过；页头角标只为没看过的异常结束提醒，避免同一次退出一直标红。
 * - `autoCreate` 表示「本次打开时若没有活跃会话需新建一个默认终端」：打开时 snapshot 可能尚未
 *   到达，由工作台在会话列表已知后执行并清除；只在打开那一刻置位，会话退出不会触发新建。
 * - Agent 把命令填进某个终端（镜像里它的 `prefillPending` 翻成 true，见 `newlyPrefilled`）时，展开面板并
 *   选中它，不发焦点请求：抢焦点会把用户在输入框里的按键（含回车）送进终端。
 *
 * 页头按钮在 SessionPage 之外，因此状态放在按 sessionId 分桶的全局 store，而不是组件 state。
 */
export type TerminalWorkbenchSessionState = {
  sessions: TerminalSessionEntry[] | null;
  open: boolean;
  activeSessionId: string | null;
  focusPending: boolean;
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
  /**
   * 用户关闭的会话已被后端收下：它仍是选中项时换到其他活跃会话，没有其他活跃会话才清掉选中并收起面板
   * （同 VS Code，再次打开会自动新建）。按此刻的状态判断而不是按点击时的标签：回执到达前用户可能已新建并
   * 选中别的会话，snapshot 也可能还没把关掉的会话移走，所以下一项只在关掉的会话之外挑。
   */
  closedByUser: (sessionId: number, terminalSessionId: string) => void;
  consumeAutoCreate: (sessionId: number) => void;
  consumeFocus: (sessionId: number) => void;
  /** Session（或其所在工作区）删除成功后丢弃该 Session 的桶，避免已删除 Session 的状态常驻。 */
  forget: (sessionIds: readonly number[]) => void;
};

const EMPTY: TerminalWorkbenchSessionState = {
  sessions: null,
  open: false,
  activeSessionId: null,
  focusPending: false,
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
        const prefilled = newlyPrefilled(current.sessions, sessions);
        const open = current.open || prefilled !== null;
        const acknowledgedEnds = open ? ended : current.acknowledgedEnds.filter((id) => ended.includes(id));
        return { bySession: { ...state.bySession, [sessionId]: {
          ...current,
          sessions,
          open,
          activeSessionId: prefilled ?? reconcileActive(sessions, current.activeSessionId),
          autoCreate: prefilled === null && current.autoCreate,
          acknowledgedEnds,
        } } };
      });
    },
    setOpen: (sessionId, open) => update(sessionId, (current) => ({
      open,
      autoCreate: open && !current.open && !hasActiveSession(current.sessions),
      focusPending: open,
      acknowledgedEnds: open ? endedIds(current.sessions) : current.acknowledgedEnds,
    })),
    toggle: (sessionId) => update(sessionId, (current) => ({
      open: !current.open,
      autoCreate: !current.open && !hasActiveSession(current.sessions),
      focusPending: !current.open,
      acknowledgedEnds: current.open ? current.acknowledgedEnds : endedIds(current.sessions),
    })),
    select: (sessionId, terminalSessionId) => update(sessionId, () => ({
      activeSessionId: terminalSessionId,
      focusPending: true,
    })),
    reveal: (sessionId, terminalSessionId) => update(sessionId, (current) => ({
      open: true,
      activeSessionId: terminalSessionId,
      autoCreate: false,
      focusPending: true,
      acknowledgedEnds: endedIds(current.sessions),
    })),
    closedByUser: (sessionId, terminalSessionId) => update(sessionId, (current) => {
      if (current.activeSessionId !== null && current.activeSessionId !== terminalSessionId) return {};
      const next = current.sessions?.find((session) => session.terminalSessionId !== terminalSessionId && isActiveTerminalSession(session));
      if (next) return { activeSessionId: next.terminalSessionId, focusPending: true };
      return { activeSessionId: null, open: false, focusPending: false };
    }),
    consumeAutoCreate: (sessionId) => update(sessionId, () => ({ autoCreate: false })),
    consumeFocus: (sessionId) => update(sessionId, () => ({ focusPending: false })),
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
