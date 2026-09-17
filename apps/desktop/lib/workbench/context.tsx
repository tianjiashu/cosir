import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useWorkbenchStore } from "./store";

type WorkbenchContextValue = {
  workspaceId: number | null;
  openAgent: (input: { taskId: number; title: string; role: string | null }) => void;
};

const WorkbenchContext = createContext<WorkbenchContextValue | null>(null);

export function WorkbenchProvider({ workspaceId, children }: { workspaceId: number | null; children: ReactNode }) {
  const ensureWorkspace = useWorkbenchStore((state) => state.ensureWorkspace);
  const openAgentTab = useWorkbenchStore((state) => state.openAgentTab);
  useEffect(() => ensureWorkspace(workspaceId), [ensureWorkspace, workspaceId]);
  return <WorkbenchContext.Provider value={{ workspaceId, openAgent: (input) => { if (workspaceId !== null) openAgentTab({ ...input, workspaceId }); } }}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbenchActions() {
  return useContext(WorkbenchContext);
}
