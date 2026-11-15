import { Fragment, type ReactNode } from "react";
import { FolderIcon } from "lucide-react";

import { AgentConfigurationPanel } from "@/components/configuration/agents/agent-configuration-panel";
import { ConfigurationPageLayout } from "@/components/configuration/configuration-page-layout";
import { configurationPath, WORKSPACE_CONFIGURATION_TABS, type WorkspaceConfigurationTabId } from "@/components/configuration/configuration-tabs";
import { FileIgnorePanel } from "@/components/configuration/documents/fileignore-panel";
import { MarkdownConfigurationPanel } from "@/components/configuration/documents/markdown-configuration-panel";
import { MCPConfigurationPanel } from "@/components/configuration/mcp/mcp-configuration-panel";
import { SkillConfigurationPanel } from "@/components/configuration/skills/skill-configuration-panel";
import { getWorkspaceInstructionConfiguration, updateWorkspaceInstructionConfiguration } from "@/lib/api/configuration";
import { queryKeys } from "@/lib/query/query-keys";

/** 标签到面板的映射；`satisfies` 保证登记处新增标签时这里必须接上面板，不会渲染出空白。 */
const PANELS = {
  agents: (workspaceId) => <AgentConfigurationPanel workspaceId={workspaceId} />,
  instructions: (workspaceId) => (
    <MarkdownConfigurationPanel
      adapter={{
        queryKey: queryKeys.instruction("workspace", workspaceId),
        label: "工作区 AGENTS.md",
        load: (options) => getWorkspaceInstructionConfiguration(workspaceId, options),
        save: (content) => updateWorkspaceInstructionConfiguration(workspaceId, content),
        saveFailedEvent: "markdown_configuration_save_failed",
      }}
      placeholder="写入当前工作区的项目约束…"
      saveLabel="保存 AGENTS.md"
    />
  ),
  fileignore: (workspaceId) => <FileIgnorePanel workspaceId={workspaceId} />,
  mcp: (workspaceId) => <MCPConfigurationPanel workspaceId={workspaceId} />,
  skills: (workspaceId, skillName) => <SkillConfigurationPanel workspaceId={workspaceId} skillName={skillName} />,
} satisfies Record<WorkspaceConfigurationTabId, (workspaceId: number, skillName?: string) => ReactNode>;

/**
 * 工作区配置（/workspaces/:id/configuration/:tab）。路由以 workspaceId 为 key 挂载本页，切换工作区不会带走草稿。
 */
export function WorkspaceConfigurationPage({ workspaceId, workspaceName, tab, skillName }: { workspaceId: number; workspaceName: string; tab: WorkspaceConfigurationTabId; skillName?: string }) {
  return (
    <ConfigurationPageLayout
      tabs={WORKSPACE_CONFIGURATION_TABS}
      basePath={configurationPath(workspaceId)}
      navLabel="工作区配置分类"
      intro={(
        <p className="text-muted-foreground flex items-center gap-2 text-xs">
          <FolderIcon className="size-3.5" aria-hidden="true" />
          以下配置只作用于工作区
          <span className="text-foreground font-medium">{workspaceName}</span>
        </p>
      )}
    >
      {/* 按标签换 key：同类面板（如两个 Markdown 文档）切换时重新挂载，草稿不串到另一份文档。 */}
      <Fragment key={tab}>{PANELS[tab](workspaceId, skillName)}</Fragment>
    </ConfigurationPageLayout>
  );
}
