import { create } from "zustand";
import type { WorkbenchTab } from "./types";

type WorkbenchState = {
  workspaceId: number | null;
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  panelOpen: boolean;
  panelWidth: number;
  ensureWorkspace: (workspaceId: number | null) => void;
  openAgentTab: (input: Omit<WorkbenchTab, "id" | "kind">) => void;
  activateTab: (tabId: string) => void;
  closeTab: (tabId: string) => void;
  closeWorkspace: (workspaceId: number) => void;
  setPanelOpen: (open: boolean) => void;
  setPanelWidth: (width: number) => void;
};

const tabId = (taskId: number): WorkbenchTab["id"] => `agent-task:${taskId}`;

export const useWorkbenchStore = create<WorkbenchState>((set) => ({
  workspaceId: null,
  tabs: [],
  activeTabId: null,
  panelOpen: false,
  panelWidth: 480,
  ensureWorkspace: (workspaceId) => set((state) => state.workspaceId === workspaceId ? state : {
    ...state, workspaceId, tabs: [], activeTabId: null, panelOpen: false,
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
  activateTab: (activeTabId) => set((state) => ({ ...state, activeTabId, panelOpen: true })),
  closeTab: (tabId) => set((state) => {
    const index = state.tabs.findIndex((tab) => tab.id === tabId);
    if (index < 0) return state;
    const tabs = state.tabs.filter((tab) => tab.id !== tabId);
    const activeTabId = state.activeTabId === tabId ? tabs[Math.min(index, tabs.length - 1)]?.id ?? null : state.activeTabId;
    return { ...state, tabs, activeTabId, panelOpen: tabs.length > 0 && state.panelOpen };
  }),
  closeWorkspace: (workspaceId) => set((state) => state.workspaceId === workspaceId ? { ...state, tabs: [], activeTabId: null, panelOpen: false } : state),
  setPanelOpen: (panelOpen) => set((state) => ({ ...state, panelOpen })),
  setPanelWidth: (panelWidth) => set((state) => ({ ...state, panelWidth: Math.max(320, Math.min(900, panelWidth)) })),
}));
