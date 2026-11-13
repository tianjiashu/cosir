import type { AgentEditorSession } from "@/lib/agent-editor/agent-editor-session";

/** 标签按工作区分桶存放（见 store），标签自身不再记录所属工作区。 */
type WorkbenchAgentTab = {
  id: `agent-session:${number}`;
  kind: "agent";
  sessionId: number;
  title: string;
};

/** 工具生成的子 Agent 配置草稿；编辑状态全部归会话，标签只负责定位与展示。 */
export type WorkbenchAgentConfigurationDraftTab = {
  id: `agent-config-draft:${string}`;
  kind: "agent-configuration-draft";
  /** 提出草稿的对话 Session；Session 删除时标签随之移除。 */
  sessionId: number;
  toolCallId: string;
  title: string;
  session: AgentEditorSession;
};

export type WorkbenchDraftTab = WorkbenchAgentConfigurationDraftTab;

/** 工作台只放子 Agent 与配置草稿；文件与内置浏览器是各自独立的停靠面板（`lib/layout/dock-store.ts`）。 */
export type WorkbenchTab =
  | WorkbenchAgentTab
  | WorkbenchDraftTab;
