import { createContext, useContext, useMemo, type ReactNode } from "react";

import { useWorkbenchStore, type AgentDraftInput, type TeamDraftInput } from "./store";

type WorkbenchContextValue = {
  /** 工作台当前显示的工作区；工具卡片据此决定能否打开标签，以及打开到哪个工作区的桶。 */
  workspaceId: number;
  openAgent: (input: { taskId: number; title: string }) => void;
  openAgentConfigurationDraft: (input: AgentDraftInput) => void;
  openAgentTeamConfigurationDraft: (input: TeamDraftInput) => void;
};

const WorkbenchContext = createContext<WorkbenchContextValue | null>(null);

/**
 * 把对话里的工具卡片绑定到当前工作区的工作台。没有工作区（归属尚未确定）时不提供动作，卡片隐藏或禁用入口。
 * 只做绑定：标签与草稿按工作区存放在 store，切换工作区不销毁任何内容。
 */
export function WorkbenchProvider({ workspaceId, children }: { workspaceId: number | null; children: ReactNode }) {
  const value = useMemo<WorkbenchContextValue | null>(() => {
    if (workspaceId === null) return null;
    const store = useWorkbenchStore.getState();
    return {
      workspaceId,
      openAgent: (input) => store.openAgent(workspaceId, input),
      openAgentConfigurationDraft: (input) => store.openAgentConfigurationDraft(workspaceId, input),
      openAgentTeamConfigurationDraft: (input) => store.openAgentTeamConfigurationDraft(workspaceId, input),
    };
  }, [workspaceId]);
  return <WorkbenchContext.Provider value={value}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbenchActions() {
  return useContext(WorkbenchContext);
}
