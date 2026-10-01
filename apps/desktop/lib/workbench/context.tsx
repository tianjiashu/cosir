import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useWorkbenchStore } from "./store";
import type { AgentConfigurationDraft } from "./types";
import type { AgentConfigurationInput } from "@/lib/api/configuration";

type WorkbenchContextValue = {
  workspaceId: number | null;
  openAgent: (input: { taskId: number; title: string; role: string | null }) => void;
  openAgentConfigurationDraft: (input: { taskId: number; toolCallId: string; draft: AgentConfigurationDraft }) => void;
};

const WorkbenchContext = createContext<WorkbenchContextValue | null>(null);

export function WorkbenchProvider({ workspaceId, children }: { workspaceId: number | null; children: ReactNode }) {
  const ensureWorkspace = useWorkbenchStore((state) => state.ensureWorkspace);
  const openAgentTab = useWorkbenchStore((state) => state.openAgentTab);
  const openAgentConfigurationDraftTab = useWorkbenchStore((state) => state.openAgentConfigurationDraftTab);
  useEffect(() => ensureWorkspace(workspaceId), [ensureWorkspace, workspaceId]);
  return <WorkbenchContext.Provider value={{
    workspaceId,
    openAgent: (input) => { if (workspaceId !== null) openAgentTab({ ...input, workspaceId }); },
    openAgentConfigurationDraft: (input) => {
      if (workspaceId !== null) {
        openAgentConfigurationDraftTab({
          ...input,
          workspaceId,
          title: input.draft.agent_id || "子 Agent 配置草稿",
          scope: "workspace",
          draft: {
            ...input.draft,
            allowed_tool_groups: [],
            max_steps: 100,
            model_config_id: null,
            model_settings: {},
          } satisfies AgentConfigurationInput,
        });
      }
    },
  }}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbenchActions() {
  return useContext(WorkbenchContext);
}
