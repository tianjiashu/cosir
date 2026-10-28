import type { AgentConfigurationInput } from "@/lib/api/configuration";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";

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

export type WorkbenchAgentTeamConfigurationDraftTab = {
  id: `agent-team-config-draft:${string}`;
  kind: "agent-team-configuration-draft";
  workspaceId: number;
  taskId: number;
  toolCallId: string;
  title: string;
  session: TeamEditorSession;
};

type WorkbenchFilesTab = {
  id: `workspace-files:${number}`;
  kind: "files";
  workspaceId: number;
  title: string;
  rootPath: string;
  workspaceName: string;
};

/** 任务概览：卡片栈（本期只有后台任务卡），内容跟随当前聚焦的 Task，每个工作区至多一个。 */
type WorkbenchTaskOverviewTab = {
  id: `task-overview:${number}`;
  kind: "task-overview";
  workspaceId: number;
  title: string;
};

export type WorkbenchTab =
  | WorkbenchFilesTab
  | WorkbenchAgentTab
  | WorkbenchAgentConfigurationDraftTab
  | WorkbenchAgentTeamConfigurationDraftTab
  | WorkbenchTaskOverviewTab;
