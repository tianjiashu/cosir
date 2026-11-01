import { useMemo } from "react";
import { FolderIcon, Loader2Icon, SaveIcon } from "lucide-react";

import { SettingsPageLayout } from "@/components/settings/settings-page-layout";
import { WORKSPACE_SETTINGS_TABS, type WorkspaceSettingsTabId } from "@/components/settings/settings-tabs";
import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { useConfigurationDocument, type ConfigurationDocumentAdapter } from "@/components/configuration/use-configuration-document";
import { AgentConfigurationPanel } from "@/components/configuration/agent-configuration-panel";
import { AgentTeamConfigurationPanel } from "@/components/agent-team/configuration-panel";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import {
  getWorkspaceFileIgnoreConfiguration,
  getWorkspaceInstructionConfiguration,
  updateWorkspaceFileIgnoreConfiguration,
  updateWorkspaceInstructionConfiguration,
  type WorkspaceFileIgnoreConfiguration,
} from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import type { MarkdownDocumentContract } from "@/lib/markdown-document";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";

function WorkspaceFileIgnorePanel({ workspaceId }: { workspaceId: number }) {
  const adapter = useMemo<ConfigurationDocumentAdapter<WorkspaceFileIgnoreConfiguration>>(() => ({
    queryKey: queryKeys.workspaceFileIgnore(workspaceId),
    label: ".fileignore",
    load: (options) => getWorkspaceFileIgnoreConfiguration(workspaceId, options),
    save: (content) => updateWorkspaceFileIgnoreConfiguration(workspaceId, content),
    saveFailedEvent: "workspace_fileignore_save_failed",
  }), [workspaceId]);
  const state = useConfigurationDocument(adapter);
  useUnsavedChanges("settings", adapter.label, state.dirty);

  if (!state.document) {
    return state.loadError
      ? <ErrorState className="my-10" message={userErrorMessage(state.loadError, "读取 .fileignore 失败")} onRetry={state.reload} retryLabel="重新读取" />
      : <LoadingState label="正在读取 .fileignore…" />;
  }
  return <div className="space-y-4">{state.saveError !== null && <Alert tone="destructive">{userErrorMessage(state.saveError, "保存 .fileignore 失败")}</Alert>}<Card className="p-4"><p className="text-muted-foreground mb-3 text-xs">控制工作区搜索时跳过哪些文件和目录。支持 gitignore 语法。</p><Textarea className="min-h-[28rem] resize-y border-0 bg-transparent p-2 font-mono text-sm shadow-none focus-visible:ring-0" value={state.content} onChange={(event) => state.setContent(event.target.value)} /><div className="mt-4 flex items-center justify-end gap-2 border-t pt-4"><Button onClick={state.save} disabled={state.saving}>{state.saving ? <><Loader2Icon className="animate-spin" />保存中…</> : <><SaveIcon />保存 .fileignore</>}</Button></div></Card></div>;
}

/**
 * 工作区配置（/workspaces/:id/settings/:tab）。路由以 workspaceId 为 key 挂载本页，切换工作区不会带走草稿。
 */
export function WorkspaceConfigurationPage({ workspaceId, workspaceName, tab }: { workspaceId: number; workspaceName: string; tab: WorkspaceSettingsTabId }) {
  const instructionAdapter = useMemo<ConfigurationDocumentAdapter<MarkdownDocumentContract>>(() => ({
    queryKey: queryKeys.instruction("workspace", workspaceId),
    label: "工作区 AGENTS.md",
    load: (options) => getWorkspaceInstructionConfiguration(workspaceId, options),
    save: (content) => updateWorkspaceInstructionConfiguration(workspaceId, content),
    saveFailedEvent: "markdown_configuration_save_failed",
  }), [workspaceId]);

  return (
    <SettingsPageLayout
      tabs={WORKSPACE_SETTINGS_TABS}
      basePath={`/workspaces/${workspaceId}/settings`}
      navLabel="工作区配置分类"
      intro={(
        <p className="text-muted-foreground flex items-center gap-2 text-xs">
          <FolderIcon className="size-3.5" aria-hidden="true" />
          以下配置只作用于工作区
          <span className="text-foreground font-medium">{workspaceName}</span>
        </p>
      )}
    >
      {tab === "agents" && <AgentConfigurationPanel workspaceId={workspaceId} />}
      {tab === "agent-teams" && <AgentTeamConfigurationPanel workspaceId={workspaceId} />}
      {tab === "instructions" && <MarkdownConfigurationPanel adapter={instructionAdapter} placeholder="写入当前工作区的项目约束…" saveLabel="保存 AGENTS.md" />}
      {tab === "fileignore" && <WorkspaceFileIgnorePanel workspaceId={workspaceId} />}
    </SettingsPageLayout>
  );
}
