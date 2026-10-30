"use client";

import type { PersistTeam } from "@/lib/agent-team-editor/session";

import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { UsersIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
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
import { confirmCloseWorkbenchTab } from "@/lib/workbench/drafts";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { queryKeys } from "@/lib/query/query-keys";
import type { WorkbenchAgentTeamConfigurationDraftTab } from "@/lib/workbench/types";

type ProfilesByScope = Record<AgentTeamConfigurationDraft["scope"], AgentConfiguration[]>;

function visibleWorkspaceProfiles(system: AgentConfiguration[], workspace: AgentConfiguration[]) {
  return [...new Map([...system, ...workspace].map((profile) => [profile.agent_id, profile])).values()];
}

/**
 * 在 Workbench 中编辑并显式保存一次工具生成的 Team 配置草稿。
 *
 * 会话和未保存守卫由工作台所有者持有，切换标签不会丢失编辑状态；
 * 保存成功的提示由编辑器统一给出，这里只负责持久化与 Team 列表缓存失效。
 */
export function AgentTeamConfigurationDraftSurface({
  workspaceId,
  tab,
  onClose,
}: {
  workspaceId: number;
  tab: WorkbenchAgentTeamConfigurationDraftTab;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const systemAgentsQuery = useQuery({
    queryKey: queryKeys.agents(null),
    queryFn: ({ signal }) => getAgentConfigurations({ signal }),
  });
  const workspaceAgentsQuery = useQuery({
    queryKey: queryKeys.agents(workspaceId),
    queryFn: ({ signal }) => getWorkspaceAgentConfigurations(workspaceId, { signal }),
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
    if (await confirmCloseWorkbenchTab(tab)) onClose();
  };

  const save: PersistTeam = async (configuration, identity) => {
    const input = { scope: configuration.scope, configuration } as const;
    const targetWorkspaceId = configuration.scope === "workspace" ? workspaceId : undefined;
    const saved = identity
      ? await updateAgentTeamConfiguration(identity.teamId, input, targetWorkspaceId)
      : await createAgentTeamConfiguration(input, targetWorkspaceId);
    void queryClient.invalidateQueries({ queryKey: queryKeys.agentTeams(targetWorkspaceId ?? null) });
    return saved;
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-start gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-2"><UsersIcon className="size-4 shrink-0" /><div className="min-w-0"><h2 className="truncate text-sm font-medium">Agent Team 配置草稿</h2><p className="text-muted-foreground truncate text-xs">对话生成，仅保存后生效</p></div></div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {profiles ? <AgentTeamConfigurationEditor
          key={tab.id}
          session={tab.session}
          profilesByScope={profiles}
          scopeEditable
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
