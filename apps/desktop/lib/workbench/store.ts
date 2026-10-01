import { create } from "zustand";
import type { WorkbenchAgentConfigurationDraftTab, WorkbenchAgentTab, WorkbenchTab } from "./types";

type WorkbenchState = {
  workspaceId: number | null;
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  savedDraftToolCallIds: string[];
  panelOpen: boolean;
  panelWidth: number;
  ensureWorkspace: (workspaceId: number | null) => void;
  openAgentTab: (input: Omit<WorkbenchAgentTab, "id" | "kind">) => void;
  openAgentConfigurationDraftTab: (input: Omit<WorkbenchAgentConfigurationDraftTab, "id" | "kind" | "dirty">) => void;
  updateAgentConfigurationDraft: (tabId: string, draft: WorkbenchAgentConfigurationDraftTab["draft"]) => void;
  setAgentConfigurationDraftScope: (tabId: string, scope: WorkbenchAgentConfigurationDraftTab["scope"]) => void;
  setDraftDirty: (tabId: string, dirty: boolean) => void;
  activateTab: (tabId: string) => void;
  closeTab: (tabId: string) => void;
  closeWorkspace: (workspaceId: number) => void;
  setPanelOpen: (open: boolean) => void;
  setPanelWidth: (width: number) => void;
};

const tabId = (taskId: number): `agent-task:${number}` => `agent-task:${taskId}`;
const draftTabId = (toolCallId: string): WorkbenchAgentConfigurationDraftTab["id"] => `agent-config-draft:${toolCallId}`;

export const useWorkbenchStore = create<WorkbenchState>((set) => ({
  workspaceId: null,
  tabs: [],
  activeTabId: null,
  savedDraftToolCallIds: [],
  panelOpen: false,
  panelWidth: 480,
  ensureWorkspace: (workspaceId) => set((state) => state.workspaceId === workspaceId ? state : {
    ...state, workspaceId, tabs: [], activeTabId: null, savedDraftToolCallIds: [], panelOpen: false,
  }),
  openAgentTab: (input) => set((state) => {
    const id = tabId(input.taskId);
    const existing = state.tabs.find((tab) => tab.id === id);
    return {
      ...state,
      workspaceId: input.workspaceId,
      tabs: existing ? state.tabs.map((tab) => tab.id === id ? { ...tab, title: input.title, role: input.role } : tab) : [...state.tabs, { ...input, id, kind: "agent" }],
      activeTabId: id,
      panelOpen: true,
    };
  }),
  openAgentConfigurationDraftTab: (input) => set((state) => {
    const id = draftTabId(input.toolCallId);
    const existing = state.tabs.find((tab) => tab.id === id);
    const nextTab: WorkbenchAgentConfigurationDraftTab = {
      ...input,
      id,
      kind: "agent-configuration-draft",
      dirty: existing?.kind === "agent-configuration-draft" ? existing.dirty : true,
    };
    const existingDraft = existing?.kind === "agent-configuration-draft" ? existing : null;
    return {
      ...state,
      savedDraftToolCallIds: existingDraft
        ? state.savedDraftToolCallIds
        : state.savedDraftToolCallIds.filter((toolCallId) => toolCallId !== input.toolCallId),
      workspaceId: input.workspaceId,
      tabs: existing
        ? existingDraft ? state.tabs : state.tabs.map((tab) => tab.id === id ? nextTab : tab)
        : [...state.tabs, nextTab],
      activeTabId: id,
      panelOpen: true,
    };
  }),
  setDraftDirty: (tabId, dirty) => set((state) => {
    const tab = state.tabs.find((candidate) => candidate.id === tabId);
    if (tab?.kind !== "agent-configuration-draft") return state;
    const savedDraftToolCallIds = new Set(state.savedDraftToolCallIds);
    if (dirty) savedDraftToolCallIds.delete(tab.toolCallId);
    else savedDraftToolCallIds.add(tab.toolCallId);
    return {
      ...state,
      savedDraftToolCallIds: [...savedDraftToolCallIds],
      tabs: state.tabs.map((candidate) => candidate.id === tabId ? { ...candidate, dirty } : candidate),
    };
  }),
  updateAgentConfigurationDraft: (tabId, draft) => set((state) => ({
    ...state,
    tabs: state.tabs.map((tab) => tab.id === tabId && tab.kind === "agent-configuration-draft" ? { ...tab, draft } : tab),
  })),
  setAgentConfigurationDraftScope: (tabId, scope) => set((state) => ({
    ...state,
    tabs: state.tabs.map((tab) => tab.id === tabId && tab.kind === "agent-configuration-draft" ? { ...tab, scope } : tab),
  })),
  activateTab: (activeTabId) => set((state) => ({ ...state, activeTabId, panelOpen: true })),
  closeTab: (tabId) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === tabId);
    if (index < 0) return state;
    const tabs = state.tabs.filter((tab) => tab.id !== tabId);
    const activeTabId = state.activeTabId === tabId ? tabs[Math.min(index, tabs.length - 1)]?.id ?? null : state.activeTabId;
    return { ...state, tabs, activeTabId, panelOpen: tabs.length > 0 && state.panelOpen };
  }),
  closeWorkspace: (workspaceId) => set((state) => state.workspaceId === workspaceId ? {
    ...state,
    tabs: [],
    activeTabId: null,
    savedDraftToolCallIds: [],
    panelOpen: false,
  } : state),
  setPanelOpen: (panelOpen) => set((state) => ({ ...state, panelOpen })),
  setPanelWidth: (panelWidth) => set((state) => ({ ...state, panelWidth: Math.max(320, Math.min(900, panelWidth)) })),
}));
