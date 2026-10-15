"use client";

import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { UsersIcon, XIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  createAgentTeamConfiguration,
  updateAgentTeamConfiguration,
  type AgentTeamConfigurationDraft,
} from "@/lib/api/agent-teams";
import {
  getAgentConfigurations,
  getWorkspaceAgentConfigurations,
  type AgentConfiguration,
} from "@/lib/api/configuration";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchAgentTeamConfigurationDraftTab } from "@/lib/workbench/types";

type ProfilesByScope = Record<AgentTeamConfigurationDraft["scope"], AgentConfiguration[]>;

function visibleWorkspaceProfiles(system: AgentConfiguration[], workspace: AgentConfiguration[]) {
  return [...new Map([...system, ...workspace].map((profile) => [profile.agent_id, profile])).values()];
}

/**
 * 在 Workbench 中编辑并显式保存一次工具生成的 Team 配置草稿。
 *
 * 草稿未保存时登记到 workbench 未保存守卫，收起工作台等离开动作会先询问；
 * 保存成功的提示由编辑器统一给出，这里只负责持久化与 Team 列表缓存失效。
 */
export function AgentTeamConfigurationDraftSurface({
  tab,
  onClose,
}: {
  tab: WorkbenchAgentTeamConfigurationDraftTab;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const updateDraft = useWorkbenchStore((state) => state.updateAgentTeamConfigurationDraft);
  const setDirty = useWorkbenchStore((state) => state.setAgentTeamDraftDirty);
  const markSaved = useWorkbenchStore((state) => state.markAgentTeamDraftSaved);
  useUnsavedChanges("workbench", "Agent Team 配置草稿", tab.dirty);

  const systemAgentsQuery = useQuery({
    queryKey: queryKeys.agents(null),
    queryFn: ({ signal }) => getAgentConfigurations({ signal }),
  });
  const workspaceAgentsQuery = useQuery({
    queryKey: queryKeys.agents(tab.workspaceId),
    queryFn: ({ signal }) => getWorkspaceAgentConfigurations(tab.workspaceId, { signal }),
  });
  const systemAgents = systemAgentsQuery.data;
  const workspaceAgents = workspaceAgentsQuery.data;
  const profiles = useMemo<ProfilesByScope | null>(() => (
    systemAgents && workspaceAgents
      ? { system: systemAgents, workspace: visibleWorkspaceProfiles(systemAgents, workspaceAgents) }
      : null
  ), [systemAgents, workspaceAgents]);
  const failedQuery = [systemAgentsQuery, workspaceAgentsQuery].find((query) => query.isError);

  const close = async () => {
    if (tab.dirty) {
      const discard = await requestConfirmation({
        title: "关闭 Team 配置草稿？",
        description: "Agent Team 配置草稿尚未保存，关闭后修改会丢失。",
        confirmLabel: "放弃并关闭",
        tone: "destructive",
        icon: "discard",
      });
      if (!discard) return;
    }
    onClose();
  };

  const save = async (configuration: AgentTeamConfigurationDraft) => {
    const input = { scope: configuration.scope, configuration } as const;
    const workspaceId = configuration.scope === "workspace" ? tab.workspaceId : undefined;
    if (tab.savedScope === null) {
      await createAgentTeamConfiguration(input, workspaceId);
    } else {
      if (configuration.scope !== tab.savedScope) {
        throw new UserFacingError("已保存的草稿不能更改保存范围");
      }
      await updateAgentTeamConfiguration(configuration.team_id, input, workspaceId);
    }
    updateDraft(tab.id, configuration);
    markSaved(tab.id, configuration.scope);
    void queryClient.invalidateQueries({ queryKey: queryKeys.agentTeams(workspaceId ?? null) });
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-2"><UsersIcon className="size-4 shrink-0" /><div className="min-w-0"><h2 className="truncate text-sm font-medium">Agent Team 配置草稿</h2><p className="text-muted-foreground truncate text-xs">对话生成，仅保存后生效</p></div></div>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭 Team 配置草稿" onClick={() => void close()}><XIcon /></Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {profiles ? <AgentTeamConfigurationEditor
          key={tab.id}
          initial={tab.draft}
          profilesByScope={profiles}
          scopeEditable={tab.savedScope === null}
          graphLayoutKey={(configuration) => `cosir:agent-team-graph:draft:${tab.id}:${configuration.scope}:${configuration.team_id || "new"}`}
          onChange={(configuration) => {
            updateDraft(tab.id, configuration);
            setDirty(tab.id, true);
          }}
          onCancel={() => void close()}
          onSave={save}
        /> : failedQuery ? <ErrorState
          message={userErrorMessage(failedQuery.error, "读取子 Agent 配置失败")}
          onRetry={() => {
            if (systemAgentsQuery.isError) void systemAgentsQuery.refetch();
            if (workspaceAgentsQuery.isError) void workspaceAgentsQuery.refetch();
          }}
        /> : <LoadingState label="正在读取子 Agent 配置…" />}
      </div>
    </section>
  );
}
