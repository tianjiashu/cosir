import { create } from "zustand";

import { createAgentEditorSession } from "@/lib/agent-editor/session";
import { createTeamEditorSession } from "@/lib/agent-team-editor/session";
import type { AgentConfigurationInput } from "@/lib/api/configuration";
import type { AgentTeamConfigurationDraft } from "@/lib/api/agent-teams";
import { clampWorkbenchWidth, readWorkbenchWidth, writeWorkbenchWidth } from "@/lib/workspace-preferences";
import type { WorkbenchDraftTab, WorkbenchTab } from "./types";

/** 一个工作区的工作台：标签、激活项与面板开合。 */
type WorkbenchSession = {
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  panelOpen: boolean;
  /** 关闭时已保存过的草稿（按 toolCallId）：只服务工具卡片在标签关闭后的「已保存」显示。 */
  savedDraftIds: readonly string[];
};

/** 工具产出的草稿：子 Agent 提案只带身份与提示词，其余字段由编辑会话补默认值。 */
export type AgentDraftInput = {
  taskId: number;
  toolCallId: string;
  draft: Pick<AgentConfigurationInput, "agent_id" | "role" | "description" | "system_prompt">;
};
export type TeamDraftInput = { taskId: number; toolCallId: string; draft: AgentTeamConfigurationDraft };

type WorkbenchState = {
  /**
   * 按工作区分桶，与文件浏览器、终端的分桶方式一致：切换工作区只是换一个桶显示，
   * 不销毁任何标签或草稿。草稿只在关闭标签（未保存时确认）与删除工作区 / Task 时丢弃。
   */
  sessions: Record<number, WorkbenchSession>;
  /** 宽度是全局用户偏好，持久化在 workspace-preferences。 */
  panelWidth: number;
  openFiles: (workspaceId: number) => void;
  openTaskOverview: (workspaceId: number) => void;
  openAgent: (workspaceId: number, input: { taskId: number; title: string }) => void;
  openAgentConfigurationDraft: (workspaceId: number, input: AgentDraftInput) => void;
  openAgentTeamConfigurationDraft: (workspaceId: number, input: TeamDraftInput) => void;
  activateTab: (workspaceId: number, tabId: string) => void;
  closeTab: (workspaceId: number, tabId: string) => void;
  setPanelOpen: (workspaceId: number, open: boolean) => void;
  forgetWorkspace: (workspaceId: number) => void;
  /** 删除 Task 后移除属于它们的子 Agent 与草稿标签。 */
  forgetTasks: (taskIds: readonly number[]) => void;
  setPanelWidth: (width: number) => void;
};

const EMPTY_WORKBENCH_SESSION: WorkbenchSession = { tabs: [], activeTabId: null, panelOpen: false, savedDraftIds: [] };

export function isDraftTab(tab: WorkbenchTab): tab is WorkbenchDraftTab {
  return tab.kind === "agent-configuration-draft" || tab.kind === "agent-team-configuration-draft";
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
      panelOpen: true,
    };
  }
  const tab = create();
  // 新会话是工具产出的原始草稿，此前关闭时记下的「已保存」不再适用。
  const savedDraftIds = isDraftTab(tab) ? session.savedDraftIds.filter((toolCallId) => toolCallId !== tab.toolCallId) : session.savedDraftIds;
  return { tabs: [...session.tabs, tab], activeTabId: id, panelOpen: true, savedDraftIds };
}

function withoutTabs(session: WorkbenchSession, removed: (tab: WorkbenchTab) => boolean): WorkbenchSession {
  const index = session.tabs.findIndex((tab) => tab.id === session.activeTabId);
  const tabs = session.tabs.filter((tab) => !removed(tab));
  if (tabs.length === session.tabs.length) return session;
  // 关闭即丢弃未保存的修改，所以只要保存过，配置文件里留下的就是已保存的版本。
  const savedDraftIds = [...new Set([
    ...session.savedDraftIds,
    ...session.tabs.flatMap((tab) => removed(tab) && isDraftTab(tab) && tab.session.store.getState().baseline !== null ? [tab.toolCallId] : []),
  ])];
  const activeTabId = tabs.some((tab) => tab.id === session.activeTabId)
    ? session.activeTabId
    : tabs[Math.min(Math.max(index, 0), tabs.length - 1)]?.id ?? null;
  return { tabs, activeTabId, panelOpen: tabs.length > 0 && session.panelOpen, savedDraftIds };
}

export const useWorkbenchStore = create<WorkbenchState>((set) => {
  const update = (workspaceId: number, change: (session: WorkbenchSession) => WorkbenchSession) => set((state) => ({
    sessions: { ...state.sessions, [workspaceId]: change(state.sessions[workspaceId] ?? EMPTY_WORKBENCH_SESSION) },
  }));
  return {
    sessions: {},
    panelWidth: readWorkbenchWidth(),
    openFiles: (workspaceId) => update(workspaceId, (session) => openTab(session, "workspace-files", () => ({
      id: "workspace-files", kind: "files", title: "文件",
    }))),
    openTaskOverview: (workspaceId) => update(workspaceId, (session) => openTab(session, "task-overview", () => ({
      id: "task-overview", kind: "task-overview", title: "任务概览",
    }))),
    openAgent: (workspaceId, { taskId, title }) => update(workspaceId, (session) => openTab(
      session,
      `agent-task:${taskId}`,
      () => ({ id: `agent-task:${taskId}`, kind: "agent", taskId, title }),
      (tab) => ({ ...tab, title }),
    )),
    openAgentConfigurationDraft: (workspaceId, { taskId, toolCallId, draft }) => update(workspaceId, (session) => openTab(
      session,
      `agent-config-draft:${toolCallId}`,
      () => ({
        id: `agent-config-draft:${toolCallId}`,
        kind: "agent-configuration-draft",
        taskId,
        toolCallId,
        title: draft.agent_id || "子 Agent 配置草稿",
        session: createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" }),
      }),
    )),
    openAgentTeamConfigurationDraft: (workspaceId, { taskId, toolCallId, draft }) => update(workspaceId, (session) => openTab(
      session,
      `agent-team-config-draft:${toolCallId}`,
      () => ({
        id: `agent-team-config-draft:${toolCallId}`,
        kind: "agent-team-configuration-draft",
        taskId,
        toolCallId,
        title: draft.name || "Agent Team 配置草稿",
        session: createTeamEditorSession({ initial: { ...draft, scope: "workspace" }, saved: false, workspaceId }),
      }),
    )),
    activateTab: (workspaceId, activeTabId) => update(workspaceId, (session) => ({ ...session, activeTabId, panelOpen: true })),
    closeTab: (workspaceId, tabId) => update(workspaceId, (session) => withoutTabs(session, (tab) => tab.id === tabId)),
    setPanelOpen: (workspaceId, panelOpen) => update(workspaceId, (session) => ({ ...session, panelOpen })),
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
    setPanelWidth: (width) => {
      const panelWidth = clampWorkbenchWidth(width);
      writeWorkbenchWidth(panelWidth);
      set({ panelWidth });
    },
  };
});

/** 当前工作区的工作台；没有工作区或尚未打开过时为空会话（稳定引用）。 */
export function useWorkbenchSession(workspaceId: number | null): WorkbenchSession {
  return useWorkbenchStore((state) => workspaceId === null ? EMPTY_WORKBENCH_SESSION : state.sessions[workspaceId] ?? EMPTY_WORKBENCH_SESSION);
}
