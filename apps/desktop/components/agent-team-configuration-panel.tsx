"use client";

import { createTeamEditorSession, type TeamEditorSession, type PersistTeam } from "@/lib/agent-team-editor/session";
import { TeamEditorUnsavedGuard } from "@/components/team-editor-unsaved-guard";
import { confirmDiscardUnsaved } from "@/lib/unsaved-changes";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BotIcon, PlusIcon, Settings2Icon, Trash2Icon, UsersIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  createAgentTeamConfiguration,
  deleteAgentTeamConfiguration,
  getSystemAgentTeamConfigurations,
  getWorkspaceAgentTeamConfigurations,
  updateAgentTeamConfiguration,
  type AgentTeamConfiguration,
} from "@/lib/api/agent-teams";
import {
  getAgentConfigurations,
  getWorkspaceAgentConfigurations,
  type AgentConfiguration,
} from "@/lib/api/configuration";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";

type Scope = AgentTeamConfiguration["scope"];
type ProfilesByScope = Record<Scope, AgentConfiguration[]>;

function requireWorkspaceId(workspaceId?: number): number {
  if (workspaceId === undefined) throw new UserFacingError("工作区不可用");
  return workspaceId;
}

function TeamConfigurationCard({
  team,
  local,
  busy,
  onEdit,
  onDelete,
}: {
  team: AgentTeamConfiguration;
  local: boolean;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <Card className="min-h-32 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="bg-muted text-foreground flex size-10 shrink-0 items-center justify-center rounded-xl">
            <UsersIcon className="size-4" />
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="truncate font-medium">{team.name}</h3>
            <Badge size="sm">{local ? "本地" : "系统继承"}</Badge>
          </div>
        </div>
        {local && (
          <div className="flex gap-1">
            <Button variant="ghost" size="icon-sm" aria-label={`编辑 ${team.name}`} onClick={onEdit}>
              <Settings2Icon />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-destructive"
              aria-label={`删除 ${team.name}`}
              disabled={busy}
              onClick={onDelete}
            >
              <Trash2Icon />
            </Button>
          </div>
        )}
      </div>

      <div className="border-border/70 mt-4 border-t pt-3">
        <div className="text-muted-foreground mb-2 flex items-center gap-2 text-xs">
          <span>成员节点</span>
          <Badge>{team.nodes.length}</Badge>
        </div>
        {team.nodes.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {team.nodes.map((node) => (
              <div
                key={node.node_id}
                className="border-border/70 bg-background flex min-w-44 max-w-64 items-center gap-2 rounded-xl border px-3 py-2"
              >
                <div className="bg-muted text-muted-foreground flex size-7 shrink-0 items-center justify-center rounded-lg">
                  <BotIcon className="size-3.5" />
                </div>
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{node.name || node.node_id}</div>
                  <div className="text-muted-foreground truncate text-2xs">
                    {node.agent_id || "未分配 Agent"}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground text-xs">尚未添加节点</p>
        )}
      </div>
    </Card>
  );
}

/**
 * 按实际文件作用域管理 Team 配置；workspace 面板展示可见配置但只编辑本地所有项。
 *
 * Team 列表与子 Agent 列表都是查询缓存（键见 queryKeys），保存 / 删除后只失效本作用域的
 * Team 列表；workspace 作用域在 workspaceId 就绪前查询保持禁用，就绪后自动读取。
 */
export function AgentTeamConfigurationPanel({
  scope,
  workspaceId,
}: {
  scope: Scope;
  workspaceId?: number;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<TeamEditorSession | null>(null);
  const workspaceReady = scope === "system" || workspaceId !== undefined;
  const teamsKey = queryKeys.agentTeams(scope === "system" ? null : workspaceId ?? null);

  const teamsQuery = useQuery({
    queryKey: teamsKey,
    queryFn: ({ signal }) => scope === "system"
      ? getSystemAgentTeamConfigurations({ signal })
      : getWorkspaceAgentTeamConfigurations(requireWorkspaceId(workspaceId), { signal }),
    enabled: workspaceReady,
  });
  const systemAgentsQuery = useQuery({
    queryKey: queryKeys.agents(null),
    queryFn: ({ signal }) => getAgentConfigurations({ signal }),
  });
  const workspaceAgentsQuery = useQuery({
    queryKey: queryKeys.agents(workspaceId ?? null),
    queryFn: ({ signal }) => getWorkspaceAgentConfigurations(requireWorkspaceId(workspaceId), { signal }),
    enabled: scope === "workspace" && workspaceId !== undefined,
  });

  const systemAgents = systemAgentsQuery.data;
  const workspaceAgents = workspaceAgentsQuery.data;
  const profiles = useMemo<ProfilesByScope | null>(() => {
    if (!systemAgents) return null;
    if (scope === "system") return { system: systemAgents, workspace: systemAgents };
    if (!workspaceAgents) return null;
    // 工作区可见 = 系统 + 工作区，同名时工作区覆盖系统。
    return {
      system: systemAgents,
      workspace: [...new Map([...systemAgents, ...workspaceAgents].map((agent) => [agent.agent_id, agent])).values()],
    };
  }, [scope, systemAgents, workspaceAgents]);

  const save: PersistTeam = async (configuration, identity) => {
    const input = { scope, configuration: { ...configuration, scope } } as const;
    const workspace = scope === "workspace" ? requireWorkspaceId(workspaceId) : undefined;
    const saved = identity
      ? await updateAgentTeamConfiguration(identity.teamId, input, workspace)
      : await createAgentTeamConfiguration(input, workspace);
    void queryClient.invalidateQueries({ queryKey: teamsKey });
    return saved;
  };

  const deleteMutation = useMutation({
    mutationFn: (team: AgentTeamConfiguration) => deleteAgentTeamConfiguration(
      team.team_id,
      scope,
      scope === "workspace" ? requireWorkspaceId(workspaceId) : undefined,
    ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: teamsKey });
      notify.success("Agent Team 配置已删除");
    },
    onError: (cause, team) => {
      void frontendLog("ERROR", "agent_team_configuration_delete_failed", "删除 Agent Team 配置失败", {
        data: { teamId: team.team_id, scope, workspaceId },
        error: cause,
      });
    },
  });

  const remove = (team: AgentTeamConfiguration) => {
    void requestConfirmation({
      title: "删除 Agent Team 配置？",
      description: `将删除 Agent Team 配置“${team.name}”。`,
      confirmLabel: "删除",
      busyLabel: "正在删除…",
      tone: "destructive",
      icon: "delete",
      action: () => deleteMutation.mutateAsync(team),
      failureFallback: "删除失败，请重试",
    });
  };

  if (editing && profiles) {
    return <><TeamEditorUnsavedGuard session={editing} scope="settings" /><AgentTeamConfigurationEditor
      session={editing}
      profilesByScope={profiles}
      scopeEditable={false}
      onCancel={() => { void confirmDiscardUnsaved("settings").then(discard => { if (discard) setEditing(null); }); }}
      onSave={save}
    /></>;
  }
  const failedQueries = [teamsQuery, systemAgentsQuery, workspaceAgentsQuery].filter((query) => query.isError);
  if (failedQueries.length > 0) {
    return <ErrorState
      className="my-12"
      message={userErrorMessage(failedQueries[0].error, "读取 Agent Team 配置失败")}
      onRetry={() => { for (const query of failedQueries) void query.refetch(); }}
      retryLabel="重新读取"
    />;
  }
  const teams = teamsQuery.data;
  if (!teams || !profiles) return <LoadingState className="pb-10" label="正在读取 Agent Team 配置…" />;

  return (
    <div className="space-y-4">
      <div className={`flex items-center gap-3 ${scope === "system" ? "justify-between" : "justify-end"}`}>
        {scope === "system" && <p className="text-muted-foreground text-sm">系统级 Team 配置</p>}
        <Button onClick={() => setEditing(createTeamEditorSession({ saved: false, scope, workspaceId }))}><PlusIcon />新建 Team</Button>
      </div>
      {teams.length === 0 ? <EmptyState title="还没有 Agent Team 配置" /> : <div className="grid gap-3">
        {teams.map((team) => {
          const local = team.scope === scope;
          return (
            <TeamConfigurationCard
              key={team.scope + ":" + team.team_id}
              team={team}
              local={local}
              busy={deleteMutation.isPending && deleteMutation.variables?.team_id === team.team_id}
              onEdit={() => setEditing(createTeamEditorSession({ initial: team, saved: true, workspaceId }))}
              onDelete={() => remove(team)}
            />
          );
        })}
      </div>}
    </div>
  );
}
