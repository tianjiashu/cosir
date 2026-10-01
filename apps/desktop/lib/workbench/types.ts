import type { AgentConfigurationInput } from "@/lib/api/configuration";

export type WorkbenchAgentTab = {
  id: `agent-task:${number}`;
  kind: "agent";
  workspaceId: number;
  taskId: number;
  title: string;
  role: string | null;
};

export type AgentConfigurationDraft = Pick<
  AgentConfigurationInput,
  "agent_id" | "role" | "description" | "system_prompt"
>;

export type WorkbenchAgentConfigurationDraftTab = {
  id: `agent-config-draft:${string}`;
  kind: "agent-configuration-draft";
  workspaceId: number;
  taskId: number;
  toolCallId: string;
  title: string;
  draft: AgentConfigurationInput;
  scope: "workspace" | "system";
  dirty: boolean;
};

export type WorkbenchTab = WorkbenchAgentTab | WorkbenchAgentConfigurationDraftTab;
