import { create } from "zustand";

import { createAgentEditorSession } from "@/lib/agent-editor/agent-editor-session";
import type { AgentConfigurationInput } from "@/lib/api/configuration";
import { useDockStore } from "@/lib/layout/dock-store";
import type { WorkbenchDraftTab, WorkbenchTab } from "./workbench-tab-types";

/**
 * 一个工作区的工作台：标签与激活项。面板显不显示是停靠区的事（`useDockStore`），不在这里：
 * 打开标签时顺带把停靠区切到工作台。
 */
type WorkbenchSession = {
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  /** 关闭时已保存过的草稿（`draftKey`）：只服务工具卡片在标签关闭后的「已保存」显示。 */
  savedDraftIds: readonly string[];
};

/** 工具产出的草稿：子 Agent 提案只带身份与提示词，其余字段由编辑会话补默认值。 */
export type AgentDraftInput = {
  taskId: number;
  toolCallId: string;
  draft: Pick<AgentConfigurationInput, "agent_id" | "role" | "description" | "system_prompt">;
};

type WorkbenchState = {
  /**
   * 按工作区分桶，与文件浏览器、终端的分桶方式一致：切换工作区只是换一个桶显示，
   * 不销毁任何标签或草稿。草稿只在关闭标签（未保存时确认）与删除工作区 / Task 时丢弃。
   */
  sessions: Record<number, WorkbenchSession>;
  openAgent: (workspaceId: number, input: { taskId: number; title: string }) => void;
  openAgentConfigurationDraft: (workspaceId: number, input: AgentDraftInput) => void;
  activateTab: (workspaceId: number, tabId: string) => void;
  closeTab: (workspaceId: number, tabId: string) => void;
  forgetWorkspace: (workspaceId: number) => void;
  /** 删除 Task 后移除属于它们的子 Agent 与草稿标签；只移除，不记「已保存」（见 `rememberSavedDraft`）。 */
  forgetTasks: (taskIds: readonly number[]) => void;
};

const EMPTY_WORKBENCH_SESSION: WorkbenchSession = { tabs: [], activeTabId: null, savedDraftIds: [] };

export function isDraftTab(tab: WorkbenchTab): tab is WorkbenchDraftTab {
  return tab.kind === "agent-configuration-draft";
}

/** 已有同 id 标签时只激活（并按 `refresh` 更新），保证同一草稿的编辑会话不被覆盖；否则新建。 */
function openTab(
  session: WorkbenchSession,
  id: string,
  create: () => WorkbenchTab,
  refresh?: (tab: WorkbenchTab) => WorkbenchTab,
): WorkbenchSession {
  const existing = session.tabs.find((tab) => tab.id === id);
  if (existing) {
    return {
      ...session,
      tabs: refresh ? session.tabs.map((tab) => tab.id === id ? refresh(tab) : tab) : session.tabs,
      activeTabId: id,
    };
  }
  const tab = create();
  // 新会话是工具产出的原始草稿，此前关闭时记下的「已保存」不再适用。
  const savedDraftIds = isDraftTab(tab) ? session.savedDraftIds.filter((key) => key !== draftKey(tab.taskId, tab.toolCallId)) : session.savedDraftIds;
  return { tabs: [...session.tabs, tab], activeTabId: id, savedDraftIds };
}

/** 移除标签并修正激活项（落到相邻标签）；没有标签被移除时返回原引用。 */
function withoutTabs(session: WorkbenchSession, removed: (tab: WorkbenchTab) => boolean): WorkbenchSession {
  const index = session.tabs.findIndex((tab) => tab.id === session.activeTabId);
  const tabs = session.tabs.filter((tab) => !removed(tab));
  if (tabs.length === session.tabs.length) return session;
  const activeTabId = tabs.some((tab) => tab.id === session.activeTabId)
    ? session.activeTabId
    : tabs[Math.min(Math.max(index, 0), tabs.length - 1)]?.id ?? null;
  return { ...session, tabs, activeTabId };
}

/**
 * 关闭草稿标签时记下「已保存」。关闭即丢弃未保存的修改，所以只要保存过，配置文件里留下的就是已保存的版本。
 * 只由关闭标签调用：删除 Task 时卡片随 Task 一起消失，标记无人读取，不记。
 */
function rememberSavedDraft(session: WorkbenchSession, closed: WorkbenchTab): WorkbenchSession {
  if (!isDraftTab(closed) || closed.session.store.getState().baseline === null) return session;
  const key = draftKey(closed.taskId, closed.toolCallId);
  if (session.savedDraftIds.includes(key)) return session;
  return { ...session, savedDraftIds: [...session.savedDraftIds, key] };
}

/** 工作台按工作区存放而调用 id 只在 Task 内唯一，草稿身份要带上 Task。 */
export const draftKey = (taskId: number, toolCallId: string) => `${taskId}:${toolCallId}`;

export const useWorkbenchStore = create<WorkbenchState>((set) => {
  const update = (workspaceId: number, change: (session: WorkbenchSession) => WorkbenchSession) => set((state) => ({
    sessions: { ...state.sessions, [workspaceId]: change(state.sessions[workspaceId] ?? EMPTY_WORKBENCH_SESSION) },
  }));
  // 打开或激活标签就是要看它：停靠区切到工作台。
  const reveal = (workspaceId: number, change: (session: WorkbenchSession) => WorkbenchSession) => {
    update(workspaceId, change);
    useDockStore.getState().show("workbench");
  };
  return {
    sessions: {},
    openAgent: (workspaceId, { taskId, title }) => reveal(workspaceId, (session) => openTab(
      session,
      `agent-task:${taskId}`,
      () => ({ id: `agent-task:${taskId}`, kind: "agent", taskId, title }),
      (tab) => ({ ...tab, title }),
    )),
    openAgentConfigurationDraft: (workspaceId, { taskId, toolCallId, draft }) => reveal(workspaceId, (session) => openTab(
      session,
      `agent-config-draft:${draftKey(taskId, toolCallId)}`,
      () => ({
        id: `agent-config-draft:${draftKey(taskId, toolCallId)}`,
        kind: "agent-configuration-draft",
        taskId,
        toolCallId,
        title: draft.agent_id || "子 Agent 配置草稿",
        session: createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" }),
      }),
    )),
    activateTab: (workspaceId, activeTabId) => update(workspaceId, (session) => ({ ...session, activeTabId })),
    closeTab: (workspaceId, tabId) => update(workspaceId, (session) => {
      const closed = session.tabs.find((tab) => tab.id === tabId);
      if (!closed) return session;
      return rememberSavedDraft(withoutTabs(session, (tab) => tab === closed), closed);
    }),
    forgetWorkspace: (workspaceId) => set((state) => {
      const sessions = { ...state.sessions };
      delete sessions[workspaceId];
      return { sessions };
    }),
    forgetTasks: (taskIds) => set((state) => ({
      sessions: Object.fromEntries(Object.entries(state.sessions).map(([workspaceId, session]) => [
        workspaceId,
        withoutTabs(session, (tab) => "taskId" in tab && taskIds.includes(tab.taskId)),
      ])),
    })),
  };
});

/** 当前工作区的工作台；没有工作区或尚未打开过时为空会话（稳定引用）。 */
export function useWorkbenchSession(workspaceId: number | null): WorkbenchSession {
  return useWorkbenchStore((state) => workspaceId === null ? EMPTY_WORKBENCH_SESSION : state.sessions[workspaceId] ?? EMPTY_WORKBENCH_SESSION);
}
