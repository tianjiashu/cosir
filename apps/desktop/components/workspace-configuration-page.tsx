"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderIcon, Loader2Icon, SaveIcon } from "lucide-react";

import { SettingsPageLayout } from "@/components/settings/settings-page-layout";
import { WORKSPACE_SETTINGS_TABS, type WorkspaceSettingsTabId } from "@/components/settings/settings-tabs";
import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { AgentConfigurationPanel, type AgentConfigurationApi } from "@/components/system-configuration-page";
import { AgentTeamConfigurationPanel } from "@/components/agent-team-configuration-panel";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import {
  createWorkspaceAgentConfiguration,
  deleteWorkspaceAgentConfiguration,
  getWorkspaceAgentConfigurations,
  getWorkspaceFileIgnoreConfiguration,
  getWorkspaceInstructionConfiguration,
  updateWorkspaceAgentConfiguration,
  updateWorkspaceFileIgnoreConfiguration,
  updateWorkspaceInstructionConfiguration,
  type WorkspaceInstructionConfiguration,
} from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import type { MarkdownConfigurationAdapter } from "@/hooks/use-markdown-configuration-document";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";

function WorkspaceFileIgnorePanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const queryKey = queryKeys.workspaceFileIgnore(workspaceId);
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => getWorkspaceFileIgnoreConfiguration(workspaceId, { signal }),
  });
  // 草稿为 null 时正文跟随服务端；后台重新读取不覆盖正在编辑的内容。
  const [draft, setDraft] = useState<string | null>(null);
  const savedContent = query.data?.content;
  useUnsavedChanges("settings", ".fileignore", draft !== null && savedContent !== undefined && draft !== savedContent);
  const mutation = useMutation({
    mutationFn: (content: string) => updateWorkspaceFileIgnoreConfiguration(workspaceId, content),
    onSuccess: (saved, submitted) => {
      queryClient.setQueryData(queryKey, saved);
      setDraft((current) => (current === submitted ? null : current));
      notify.success(".fileignore 已保存");
    },
    onError: (error) => {
      void frontendLog("ERROR", "workspace_fileignore_save_failed", "保存 .fileignore 失败", { data: { workspaceId }, error });
    },
  });

  if (savedContent === undefined) {
    return query.isError
      ? <ErrorState className="my-10" message={userErrorMessage(query.error, "读取 .fileignore 失败")} onRetry={() => void query.refetch()} retryLabel="重新读取" />
      : <LoadingState label="正在读取 .fileignore…" />;
  }
  const content = draft ?? savedContent;
  return <div className="space-y-4">{mutation.isError && <Alert tone="destructive">{userErrorMessage(mutation.error, "保存 .fileignore 失败")}</Alert>}<Card className="p-4"><p className="text-muted-foreground mb-3 text-xs">控制工作区搜索时跳过哪些文件和目录。支持 gitignore 语法。</p><Textarea className="min-h-[28rem] resize-y border-0 bg-transparent p-2 font-mono text-sm shadow-none focus-visible:ring-0" value={content} onChange={(event) => { setDraft(event.target.value); if (mutation.isError) mutation.reset(); }} /><div className="mt-4 flex items-center justify-end gap-2 border-t pt-4"><Button onClick={() => mutation.mutate(content)} disabled={mutation.isPending}>{mutation.isPending ? <><Loader2Icon className="animate-spin" />保存中…</> : <><SaveIcon />保存 .fileignore</>}</Button></div></Card></div>;
}

/**
 * 工作区配置（/workspaces/:id/settings/:tab）。面板以 workspaceId 为 key，切换工作区不会带走草稿。
 */
export function WorkspaceConfigurationPage({ workspaceId, workspaceName, tab }: { workspaceId: number; workspaceName: string; tab: WorkspaceSettingsTabId }) {
  const agentApi = useMemo<AgentConfigurationApi>(() => ({
    workspaceId,
    list: (options) => getWorkspaceAgentConfigurations(workspaceId, options),
    create: (input) => createWorkspaceAgentConfiguration(workspaceId, input),
    update: (agentId, input) => updateWorkspaceAgentConfiguration(workspaceId, agentId, input),
    remove: (agentId) => deleteWorkspaceAgentConfiguration(workspaceId, agentId),
  }), [workspaceId]);
  const instructionAdapter = useMemo<MarkdownConfigurationAdapter<WorkspaceInstructionConfiguration>>(() => ({
    queryKey: queryKeys.instruction("workspace", workspaceId),
    label: "工作区 AGENTS.md",
    load: (options) => getWorkspaceInstructionConfiguration(workspaceId, options),
    save: (content: string) => updateWorkspaceInstructionConfiguration(workspaceId, content),
    allowEmpty: true,
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
      {tab === "agents" && <AgentConfigurationPanel api={agentApi} />}
      {tab === "agent-teams" && <AgentTeamConfigurationPanel scope="workspace" workspaceId={workspaceId} />}
      {tab === "instructions" && <MarkdownConfigurationPanel key={workspaceId} adapter={instructionAdapter} placeholder="写入当前工作区的项目约束…" saveLabel="保存 AGENTS.md" initialMode="split" />}
      {tab === "fileignore" && <WorkspaceFileIgnorePanel key={workspaceId} workspaceId={workspaceId} />}
    </SettingsPageLayout>
  );
}
