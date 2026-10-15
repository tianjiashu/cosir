"use client";

import { useEffect, useState } from "react";
import { BotIcon, PlusIcon, Settings2Icon, Trash2Icon, UsersIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
import { Alert } from "@/components/ui/alert";
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
  type AgentTeamConfigurationDraft,
} from "@/lib/api/agent-teams";
import {
  getAgentConfigurations,
  getWorkspaceAgentConfigurations,
  type AgentConfiguration,
} from "@/lib/api/configuration";

type Scope = AgentTeamConfiguration["scope"];
type ProfilesByScope = Record<Scope, AgentConfiguration[]>;

function requireWorkspaceId(workspaceId?: number): number {
  if (workspaceId === undefined) throw new Error("工作区不可用");
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

/** 按实际文件作用域管理 Team 配置；workspace 面板展示可见配置但只编辑本地所有项。 */
export function AgentTeamConfigurationPanel({
  scope,
  workspaceId,
}: {
  scope: Scope;
  workspaceId?: number;
}) {
  const [teams, setTeams] = useState<AgentTeamConfiguration[] | null>(null);
  const [profiles, setProfiles] = useState<ProfilesByScope | null>(null);
  const [editing, setEditing] = useState<AgentTeamConfiguration | AgentTeamConfigurationDraft | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busyTeamId, setBusyTeamId] = useState<string | null>(null);

  const load = async () => {
    if (scope === "workspace" && workspaceId === undefined) return;
    try {
      setError(null);
      const [nextTeams, systemAgents, workspaceAgents] = await Promise.all([
        scope === "system"
          ? getSystemAgentTeamConfigurations()
          : getWorkspaceAgentTeamConfigurations(requireWorkspaceId(workspaceId)),
        getAgentConfigurations(),
        scope === "workspace"
          ? getWorkspaceAgentConfigurations(requireWorkspaceId(workspaceId))
          : Promise.resolve([] as AgentConfiguration[]),
      ]);
      setTeams(nextTeams);
      setProfiles({
        system: systemAgents,
        workspace: [...new Map([...systemAgents, ...workspaceAgents].map((agent) => [agent.agent_id, agent])).values()],
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 Agent Team 配置失败");
    }
  };

  useEffect(() => { void load(); }, [scope, workspaceId]);

  const save = async (configuration: AgentTeamConfigurationDraft) => {
    const input = {
      scope,
      configuration: { ...configuration, scope },
    } as const;
    if (editing) {
      await updateAgentTeamConfiguration(
        editing.team_id,
        input,
        scope === "workspace" ? requireWorkspaceId(workspaceId) : undefined,
      );
    } else {
      await createAgentTeamConfiguration(
        input,
        scope === "workspace" ? requireWorkspaceId(workspaceId) : undefined,
      );
    }
    await load();
    setEditing({ ...configuration, scope });
  };

  const remove = async (team: AgentTeamConfiguration) => {
    if (team.scope !== scope) return;
    if (!window.confirm("删除 Agent Team 配置“" + team.name + "”？")) return;
    setBusyTeamId(team.team_id);
    setError(null);
    try {
      await deleteAgentTeamConfiguration(
        team.team_id,
        scope,
        scope === "workspace" ? requireWorkspaceId(workspaceId) : undefined,
      );
      await load();
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除失败");
    } finally {
      setBusyTeamId(null);
    }
  };

  if (editing !== undefined && profiles) {
    return <AgentTeamConfigurationEditor
      initial={editing}
      profilesByScope={profiles}
      scopeEditable={false}
      defaultScope={scope}
      graphLayoutKey={(configuration) => `cosir:agent-team-graph:${configuration.scope}:${workspaceId ?? "system"}:${configuration.team_id || "new"}`}
      onCancel={() => setEditing(undefined)}
      onSave={save}
    />;
  }
  if (!teams || !profiles) {
    if (error) return <ErrorState className="my-12" message={error} onRetry={() => void load()} retryLabel="重新读取" />;
    // workspaceId 尚未就绪时 load 直接返回，保留手动重试入口避免卡在加载态。
    return <div className="space-y-3 pb-10"><LoadingState label="正在读取 Agent Team 配置…" /><div className="flex justify-center"><Button variant="outline" onClick={() => void load()}>重新读取</Button></div></div>;
  }

  return (
    <div className="space-y-4">
      {error && <Alert tone="destructive">{error}</Alert>}
      {saved && <Alert tone="success">配置更改成功</Alert>}
      <div className={`flex items-center gap-3 ${scope === "system" ? "justify-between" : "justify-end"}`}>
        {scope === "system" && <p className="text-muted-foreground text-sm">系统级 Team 配置</p>}
        <Button onClick={() => { setSaved(false); setEditing(null); }}><PlusIcon />新建 Team</Button>
      </div>
      {teams.length === 0 ? <EmptyState title="还没有 Agent Team 配置" /> : <div className="grid gap-3">
        {teams.map((team) => {
          const local = team.scope === scope;
          return (
            <TeamConfigurationCard
              key={team.scope + ":" + team.team_id}
              team={team}
              local={local}
              busy={busyTeamId === team.team_id}
              onEdit={() => { setSaved(false); setEditing(team); }}
              onDelete={() => void remove(team)}
            />
          );
        })}
      </div>}
    </div>
  );
}
