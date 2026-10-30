import type { AgentEditorSession } from "@/lib/agent-editor/session";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";

/** 标签按工作区分桶存放（见 store），标签自身不再记录所属工作区。 */
type WorkbenchAgentTab = {
  id: `agent-task:${number}`;
  kind: "agent";
  taskId: number;
  title: string;
};

/** 工具生成的子 Agent 配置草稿；编辑状态全部归会话，标签只负责定位与展示。 */
export type WorkbenchAgentConfigurationDraftTab = {
  id: `agent-config-draft:${string}`;
  kind: "agent-configuration-draft";
  /** 提出草稿的对话 Task；Task 删除时标签随之移除。 */
  taskId: number;
  toolCallId: string;
  title: string;
  session: AgentEditorSession;
};

export type WorkbenchAgentTeamConfigurationDraftTab = {
  id: `agent-team-config-draft:${string}`;
  kind: "agent-team-configuration-draft";
  taskId: number;
  toolCallId: string;
  title: string;
  session: TeamEditorSession;
};

type WorkbenchFilesTab = {
  id: "workspace-files";
  kind: "files";
  title: string;
};

export type WorkbenchDraftTab = WorkbenchAgentConfigurationDraftTab | WorkbenchAgentTeamConfigurationDraftTab;

export type WorkbenchTab =
  | WorkbenchFilesTab
  | WorkbenchAgentTab
  | WorkbenchDraftTab;
