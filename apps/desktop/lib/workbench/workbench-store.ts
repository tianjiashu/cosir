import { create } from "zustand";

import { createAgentEditorSession } from "@/lib/agent-editor/agent-editor-session";
import type { AgentConfigurationInput } from "@/lib/api/configuration";
import { useDockStore } from "@/lib/layout/dock-store";
import type { WorkbenchDraftTab, WorkbenchTab } from "./workbench-tab-types";

/**
 * 一个工作区的工作台：标签与激活项。面板显不显示是停靠区的事（`useDockStore`），不在这里：
 * 打开标签时顺带把停靠区切到工作台。
 */
type WorkspaceWorkbench = {
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  /** 关闭时已保存过的草稿（`draftKey`）：只服务工具卡片在标签关闭后的「已保存」显示。 */
  savedDraftIds: readonly string[];
};

/** 工具产出的草稿：子 Agent 提案只带身份与提示词，其余字段由编辑会话补默认值。 */
export type AgentDraftInput = {
  sessionId: number;
  toolCallId: string;
  draft: Pick<AgentConfigurationInput, "agent_id" | "role" | "description" | "system_prompt">;
};

type WorkbenchState = {
  /**
   * 按工作区分桶，与文件浏览器一致（终端按 Session 分桶）：切换工作区只是换一个桶显示，
   * 不销毁任何标签或草稿。草稿只在关闭标签（未保存时确认）与删除工作区 / Session 时丢弃。
   */
  byWorkspace: Record<number, WorkspaceWorkbench>;
  openAgent: (workspaceId: number, input: { sessionId: number; title: string }) => void;
  openAgentConfigurationDraft: (workspaceId: number, input: AgentDraftInput) => void;
  activateTab: (workspaceId: number, tabId: string) => void;
  closeTab: (workspaceId: number, tabId: string) => void;
  forgetWorkspace: (workspaceId: number) => void;
  /** 删除 Session 后移除属于它们的子 Agent 与草稿标签；只移除，不记「已保存」（见 `rememberSavedDraft`）。 */
  forgetSessions: (sessionIds: readonly number[]) => void;
};

const EMPTY_WORKSPACE_WORKBENCH: WorkspaceWorkbench = { tabs: [], activeTabId: null, savedDraftIds: [] };

export function isDraftTab(tab: WorkbenchTab): tab is WorkbenchDraftTab {
  return tab.kind === "agent-configuration-draft";
}

/** 已有同 id 标签时只激活（并按 `refresh` 更新），保证同一草稿的编辑会话不被覆盖；否则新建。 */
function openTab(
  workbench: WorkspaceWorkbench,
  id: string,
  create: () => WorkbenchTab,
  refresh?: (tab: WorkbenchTab) => WorkbenchTab,
): WorkspaceWorkbench {
  const existing = workbench.tabs.find((tab) => tab.id === id);
  if (existing) {
    return {
      ...workbench,
      tabs: refresh ? workbench.tabs.map((tab) => tab.id === id ? refresh(tab) : tab) : workbench.tabs,
      activeTabId: id,
    };
  }
  const tab = create();
  // 新会话是工具产出的原始草稿，此前关闭时记下的「已保存」不再适用。
  const savedDraftIds = isDraftTab(tab) ? workbench.savedDraftIds.filter((key) => key !== draftKey(tab.sessionId, tab.toolCallId)) : workbench.savedDraftIds;
  return { tabs: [...workbench.tabs, tab], activeTabId: id, savedDraftIds };
}

/** 移除标签并修正激活项（落到相邻标签）；没有标签被移除时返回原引用。 */
function withoutTabs(workbench: WorkspaceWorkbench, removed: (tab: WorkbenchTab) => boolean): WorkspaceWorkbench {
  const index = workbench.tabs.findIndex((tab) => tab.id === workbench.activeTabId);
  const tabs = workbench.tabs.filter((tab) => !removed(tab));
  if (tabs.length === workbench.tabs.length) return workbench;
  const activeTabId = tabs.some((tab) => tab.id === workbench.activeTabId)
    ? workbench.activeTabId
    : tabs[Math.min(Math.max(index, 0), tabs.length - 1)]?.id ?? null;
  return { ...workbench, tabs, activeTabId };
}

/**
 * 关闭草稿标签时记下「已保存」。关闭即丢弃未保存的修改，所以只要保存过，配置文件里留下的就是已保存的版本。
 * 只由关闭标签调用：删除 Session 时卡片随 Session 一起消失，标记无人读取，不记。
 */
function rememberSavedDraft(workbench: WorkspaceWorkbench, closed: WorkbenchTab): WorkspaceWorkbench {
  if (!isDraftTab(closed) || closed.session.store.getState().baseline === null) return workbench;
  const key = draftKey(closed.sessionId, closed.toolCallId);
  if (workbench.savedDraftIds.includes(key)) return workbench;
  return { ...workbench, savedDraftIds: [...workbench.savedDraftIds, key] };
}

/** 工作台按工作区存放而调用 id 只在 Session 内唯一，草稿身份要带上 Session。 */
export const draftKey = (sessionId: number, toolCallId: string) => `${sessionId}:${toolCallId}`;

export const useWorkbenchStore = create<WorkbenchState>((set) => {
  const update = (workspaceId: number, change: (workbench: WorkspaceWorkbench) => WorkspaceWorkbench) => set((state) => ({
    byWorkspace: { ...state.byWorkspace, [workspaceId]: change(state.byWorkspace[workspaceId] ?? EMPTY_WORKSPACE_WORKBENCH) },
  }));
  // 打开或激活标签就是要看它：停靠区切到工作台。
  const reveal = (workspaceId: number, change: (workbench: WorkspaceWorkbench) => WorkspaceWorkbench) => {
    update(workspaceId, change);
    useDockStore.getState().show("workbench");
  };
  return {
    byWorkspace: {},
    openAgent: (workspaceId, { sessionId, title }) => reveal(workspaceId, (workbench) => openTab(
      workbench,
      `agent-session:${sessionId}`,
      () => ({ id: `agent-session:${sessionId}`, kind: "agent", sessionId, title }),
      (tab) => ({ ...tab, title }),
    )),
    openAgentConfigurationDraft: (workspaceId, { sessionId, toolCallId, draft }) => reveal(workspaceId, (workbench) => openTab(
      workbench,
      `agent-config-draft:${draftKey(sessionId, toolCallId)}`,
      () => ({
        id: `agent-config-draft:${draftKey(sessionId, toolCallId)}`,
        kind: "agent-configuration-draft",
        sessionId,
        toolCallId,
        title: draft.agent_id || "子 Agent 配置草稿",
        session: createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" }),
      }),
    )),
    activateTab: (workspaceId, activeTabId) => update(workspaceId, (workbench) => ({ ...workbench, activeTabId })),
    closeTab: (workspaceId, tabId) => update(workspaceId, (workbench) => {
      const closed = workbench.tabs.find((tab) => tab.id === tabId);
      if (!closed) return workbench;
      return rememberSavedDraft(withoutTabs(workbench, (tab) => tab === closed), closed);
    }),
    forgetWorkspace: (workspaceId) => set((state) => {
      const byWorkspace = { ...state.byWorkspace };
      delete byWorkspace[workspaceId];
      return { byWorkspace };
    }),
    forgetSessions: (sessionIds) => set((state) => ({
      byWorkspace: Object.fromEntries(Object.entries(state.byWorkspace).map(([workspaceId, workbench]) => [
        workspaceId,
        withoutTabs(workbench, (tab) => "sessionId" in tab && sessionIds.includes(tab.sessionId)),
      ])),
    })),
  };
});

/** 当前工作区的工作台；没有工作区或尚未打开过时为空工作台（稳定引用）。 */
export function useWorkspaceWorkbench(workspaceId: number | null): WorkspaceWorkbench {
  return useWorkbenchStore((state) => workspaceId === null ? EMPTY_WORKSPACE_WORKBENCH : state.byWorkspace[workspaceId] ?? EMPTY_WORKSPACE_WORKBENCH);
}
