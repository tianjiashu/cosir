import { Fragment, type ReactNode } from "react";

import { AgentConfigurationPanel } from "@/components/configuration/agents/agent-configuration-panel";
import { AppearancePanel } from "@/components/configuration/appearance/appearance-panel";
import { ConfigurationPageLayout } from "@/components/configuration/configuration-page-layout";
import { configurationPath, SYSTEM_CONFIGURATION_TABS, type SystemConfigurationTabId } from "@/components/configuration/configuration-tabs";
import { MarkdownConfigurationPanel } from "@/components/configuration/documents/markdown-configuration-panel";
import type { ConfigurationDocumentAdapter } from "@/components/configuration/documents/use-configuration-document";
import { EnvironmentPanel } from "@/components/configuration/environment/environment-panel";
import { MCPConfigurationPanel } from "@/components/configuration/mcp/mcp-configuration-panel";
import { ModelConnectionsPanel } from "@/components/configuration/models/model-connections-panel";
import { SkillConfigurationPanel } from "@/components/configuration/skills/skill-configuration-panel";
import {
  getGlobalInstructionConfiguration,
  getMainAgentPromptConfiguration,
  type InstructionDocument,
  updateGlobalInstructionConfiguration,
  updateMainAgentPromptConfiguration,
} from "@/lib/api/configuration";
import { queryKeys } from "@/lib/query/query-keys";

const globalInstructionAdapter: ConfigurationDocumentAdapter<InstructionDocument> = {
  queryKey: queryKeys.instruction("global"),
  label: "全局 AGENTS.md",
  load: getGlobalInstructionConfiguration,
  save: updateGlobalInstructionConfiguration,
  saveFailedEvent: "markdown_configuration_save_failed",
};

const mainAgentPromptAdapter: ConfigurationDocumentAdapter<InstructionDocument> = {
  queryKey: queryKeys.instruction("main-agent-prompt"),
  label: "主 Agent 提示词",
  load: getMainAgentPromptConfiguration,
  save: updateMainAgentPromptConfiguration,
  saveFailedEvent: "markdown_configuration_save_failed",
};

/** 标签到面板的映射；`satisfies` 保证登记处新增标签时这里必须接上面板，不会渲染出空白。 */
const PANELS = {
  appearance: () => <AppearancePanel />,
  agents: () => <AgentConfigurationPanel workspaceId={null} />,
  models: () => <ModelConnectionsPanel />,
  "main-agent": () => <MarkdownConfigurationPanel adapter={mainAgentPromptAdapter} requireContent description="保存后对新会话生效。" placeholder="定义主 Agent 的执行协议…" saveLabel="保存主 Agent 提示词" />,
  instructions: () => <MarkdownConfigurationPanel adapter={globalInstructionAdapter} description="保存后对新会话生效。" placeholder="在这里写入系统级工作约束…" saveLabel="保存全局 AGENTS.md" />,
  environment: () => <EnvironmentPanel />,
  mcp: () => <MCPConfigurationPanel workspaceId={null} />,
  skills: (skillName) => <SkillConfigurationPanel workspaceId={null} skillName={skillName} />,
} satisfies Record<SystemConfigurationTabId, (skillName?: string) => ReactNode>;

/** 系统配置中心（/configuration/:tab）。标签来自路由，布局与导航由 ConfigurationPageLayout 统一提供。 */
export function SystemConfigurationPage({ tab, skillName }: { tab: SystemConfigurationTabId; skillName?: string }) {
  return (
    <ConfigurationPageLayout tabs={SYSTEM_CONFIGURATION_TABS} basePath={configurationPath(null)} navLabel="系统配置分类">
      {/* 按标签换 key：同类面板（如两个 Markdown 文档）切换时重新挂载，草稿不串到另一份文档。 */}
      <Fragment key={tab}>{PANELS[tab](skillName)}</Fragment>
    </ConfigurationPageLayout>
  );
}
