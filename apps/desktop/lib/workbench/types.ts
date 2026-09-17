export type WorkbenchAgentTab = {
  id: `agent-task:${number}`;
  kind: "agent";
  workspaceId: number;
  taskId: number;
  title: string;
  role: string | null;
};

export type WorkbenchTab = WorkbenchAgentTab;
