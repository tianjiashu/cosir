import { create } from "zustand";

import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { isActiveTerminalSession } from "@/lib/terminal/terminal-sessions";

/**
 * 每个 Task 的终端工作台 UI 状态。
 *
 * - `sessions` 是 snapshot `terminal_sessions` 的只读镜像（由 Task 的 Assistant 会话在每次提交
 *   state 时写入），null 表示本页还没收到 snapshot；它不是事实源，也不被前端修改。离开 Task 页时
 *   由 TaskPage 置回 null（不能挂在 Assistant 会话卸载上：渲染恢复会重挂会话而终端面板不动，清空
 *   会卸掉正在使用的 xterm），再回来时不按过期列表挂载窗格、判定自动新建。
 * - `open` / `activeSessionId` 是纯 UI 偏好：每个 Task 独立记住，切走再回来恢复；不持久化。
 * - `focusToken` 递增即请求把焦点放到当前终端（打开面板、切换标签、从卡片跳转、交接）。
 * - `autoCreate` 表示「本次打开时若没有活跃会话需新建一个默认终端」：打开时 snapshot 可能尚未
 *   到达，由工作台在会话列表已知后执行并清除；只在打开那一刻置位，会话退出不会触发新建。
 *
 * 页头按钮在 TaskPage 之外，因此状态放在按 taskId 分桶的全局 store，而不是组件 state。
 */
export type TerminalWorkbenchTaskState = {
  sessions: TerminalSessionEntry[] | null;
  open: boolean;
  activeSessionId: string | null;
  focusToken: number;
  autoCreate: boolean;
};

type TerminalWorkbenchStore = {
  tasks: Record<number, TerminalWorkbenchTaskState>;
  /** 写入 snapshot 镜像；null 只清空镜像（保留面板偏好），对没有桶的 Task（如已 forget）无动作。 */
  syncSessions: (taskId: number, sessions: TerminalSessionEntry[] | null) => void;
  setOpen: (taskId: number, open: boolean) => void;
  toggle: (taskId: number) => void;
  select: (taskId: number, sessionId: string | null) => void;
  /** 打开面板、选中会话并请求焦点：工具卡片「在终端中查看」与交接共用。 */
  reveal: (taskId: number, sessionId: string) => void;
  consumeAutoCreate: (taskId: number) => void;
  /** Task（或其所在工作区）删除成功后丢弃该 Task 的桶，避免已删除 Task 的状态常驻。 */
  forget: (taskIds: readonly number[]) => void;
};

const EMPTY: TerminalWorkbenchTaskState = {
  sessions: null,
  open: false,
  activeSessionId: null,
  focusToken: 0,
  autoCreate: false,
};

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
  if (activeSessionId !== null && sessions.some((session) => session.sessionId === activeSessionId)) return activeSessionId;
  return sessions.find(isActiveTerminalSession)?.sessionId ?? null;
}

export const useTerminalWorkbenchStore = create<TerminalWorkbenchStore>((set) => {
  const update = (taskId: number, patch: (current: TerminalWorkbenchTaskState) => Partial<TerminalWorkbenchTaskState>) => (
    set((state) => {
      const current = state.tasks[taskId] ?? EMPTY;
      return { tasks: { ...state.tasks, [taskId]: { ...current, ...patch(current) } } };
    })
  );
  return {
    tasks: {},
    syncSessions: (taskId, sessions) => {
      if (sessions === null) {
        set((state) => {
          const current = state.tasks[taskId];
          if (current?.sessions == null) return state;
          return { tasks: { ...state.tasks, [taskId]: { ...current, sessions: null } } };
        });
        return;
      }
      update(taskId, (current) => {
        if (current.sessions === sessions) return {};
        const previous = new Map((current.sessions ?? []).map((session) => [session.sessionId, session]));
        // 交接是唯一自动弹出面板的时机：某会话的 handoff 从无到有时打开并聚焦它。Agent 新开会话不弹出。
        const handoff = sessions.find((session) => session.handoff !== null && !previous.get(session.sessionId)?.handoff);
        if (handoff) {
          return {
            sessions,
            open: true,
            activeSessionId: handoff.sessionId,
            focusToken: current.focusToken + 1,
            autoCreate: false,
          };
        }
        return { sessions, activeSessionId: reconcileActive(sessions, current.activeSessionId) };
      });
    },
    setOpen: (taskId, open) => update(taskId, (current) => ({
      open,
      autoCreate: open && !current.open && !hasActiveSession(current.sessions),
      focusToken: open ? current.focusToken + 1 : current.focusToken,
    })),
    toggle: (taskId) => update(taskId, (current) => ({
      open: !current.open,
      autoCreate: !current.open && !hasActiveSession(current.sessions),
      focusToken: current.open ? current.focusToken : current.focusToken + 1,
    })),
    select: (taskId, sessionId) => update(taskId, (current) => ({
      activeSessionId: sessionId,
      focusToken: current.focusToken + 1,
    })),
    reveal: (taskId, sessionId) => update(taskId, (current) => ({
      open: true,
      activeSessionId: sessionId,
      autoCreate: false,
      focusToken: current.focusToken + 1,
    })),
    consumeAutoCreate: (taskId) => update(taskId, () => ({ autoCreate: false })),
    forget: (taskIds) => set((state) => {
      if (!taskIds.some((taskId) => taskId in state.tasks)) return state;
      const tasks = { ...state.tasks };
      for (const taskId of taskIds) delete tasks[taskId];
      return { tasks };
    }),
  };
});

/** 读取某 Task 的工作台状态；未出现过的 Task 返回稳定的空状态（避免 selector 每次新建对象）。 */
export function selectTerminalTask(taskId: number) {
  return (state: { tasks: Record<number, TerminalWorkbenchTaskState> }) => state.tasks[taskId] ?? EMPTY;
}

/** 页头角标：活跃会话数；snapshot 未到时为 0。 */
export function selectActiveSessionCount(taskId: number) {
  return (state: { tasks: Record<number, TerminalWorkbenchTaskState> }) => (
    state.tasks[taskId]?.sessions?.filter(isActiveTerminalSession).length ?? 0
  );
}
