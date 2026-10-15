"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeftIcon, Code2Icon, FileTextIcon, FilterIcon, FolderIcon, Loader2Icon, SaveIcon, UsersIcon } from "lucide-react";

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
  type AgentConfigurationInput,
  type WorkspaceInstructionConfiguration,
} from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import type { MarkdownConfigurationAdapter } from "@/hooks/use-markdown-configuration-document";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";

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

export function WorkspaceConfigurationPage({ workspaceId, workspaceName, onClose }: { workspaceId: number; workspaceName: string; onClose: () => void }) {
  const [tab, setTab] = useState<"agents" | "agent-teams" | "instructions" | "fileignore">("agents");
  const agentApi = useMemo<AgentConfigurationApi>(() => ({
    workspaceId,
    list: (options) => getWorkspaceAgentConfigurations(workspaceId, options),
    create: (input: AgentConfigurationInput) => createWorkspaceAgentConfiguration(workspaceId, input),
    update: (agentId: string, input: AgentConfigurationInput) => updateWorkspaceAgentConfiguration(workspaceId, agentId, input),
    remove: (agentId: string) => deleteWorkspaceAgentConfiguration(workspaceId, agentId),
  }), [workspaceId]);
  const instructionAdapter = useMemo<MarkdownConfigurationAdapter<WorkspaceInstructionConfiguration>>(() => ({
    queryKey: queryKeys.instruction("workspace", workspaceId),
    label: "工作区 AGENTS.md",
    load: (options) => getWorkspaceInstructionConfiguration(workspaceId, options),
    save: (content: string) => updateWorkspaceInstructionConfiguration(workspaceId, content),
    allowEmpty: true,
  }), [workspaceId]);

  // 切换标签与返回都会卸载当前编辑器，先确认是否放弃未保存的修改。
  const selectTab = async (next: typeof tab) => {
    if (next === tab || !(await confirmDiscardUnsaved("settings"))) return;
    setTab(next);
  };
  const close = async () => {
    if (await confirmDiscardUnsaved("settings")) onClose();
  };

  const tabs = [
    { id: "agents" as const, label: "子 Agent", icon: Code2Icon },
    { id: "agent-teams" as const, label: "Agent Team", icon: UsersIcon },
    { id: "instructions" as const, label: "AGENTS.md", icon: FileTextIcon },
    { id: "fileignore" as const, label: ".fileignore", icon: FilterIcon },
  ];
  return (
    <div className="bg-background/95 absolute inset-0 z-30 overflow-y-auto backdrop-blur-sm">
      <div className="mx-auto flex min-h-full w-full max-w-6xl flex-col px-5 py-6 lg:px-10">
        <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">配置当前工作区</h1>
            <div className="bg-muted/70 text-muted-foreground mt-3 inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs">
              <FolderIcon className="size-3.5" />
              <span>当前工作区</span>
              <span className="text-foreground font-medium">{workspaceName}</span>
            </div>
          </div>
          <Button variant="outline" onClick={() => void close()}><ChevronLeftIcon />返回工作区</Button>
        </header>
        <div className="grid min-h-0 flex-1 gap-8 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <nav className="flex gap-2 overflow-x-auto lg:block lg:space-y-2" aria-label="工作区配置分类">
            {tabs.map(({ id, label, icon: Icon }) => (
              <button key={id} type="button" onClick={() => void selectTab(id)} className={`flex min-w-44 items-center gap-3 rounded-xl px-3 py-3 text-left transition-colors lg:w-full ${tab === id ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
                <Icon className="size-4 shrink-0" />
                <span className="text-sm font-medium">{label}</span>
              </button>
            ))}
          </nav>
          <main className="min-w-0">
            {tab === "agents" && <AgentConfigurationPanel api={agentApi} />}
            {tab === "agent-teams" && <AgentTeamConfigurationPanel scope="workspace" workspaceId={workspaceId} />}
            {tab === "instructions" && <MarkdownConfigurationPanel key={workspaceId} adapter={instructionAdapter} placeholder="写入当前 workspace 的项目约束…" saveLabel="保存 AGENTS.md" initialMode="split" />}
            {tab === "fileignore" && <WorkspaceFileIgnorePanel key={workspaceId} workspaceId={workspaceId} />}
          </main>
        </div>
      </div>
    </div>
  );
}
