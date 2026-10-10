"use client";

import { useEffect, useState } from "react";
import { BotIcon, PlusIcon, Settings2Icon, Trash2Icon, UsersIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
import { Button } from "@/components/ui/button";
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
    <article className="border-border/70 bg-card/70 min-h-32 rounded-2xl border p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="bg-muted text-foreground flex size-10 shrink-0 items-center justify-center rounded-xl">
            <UsersIcon className="size-4" />
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="truncate font-medium">{team.name}</h3>
            <span className="bg-muted text-muted-foreground shrink-0 rounded-full px-2 py-0.5 text-[10px]">
              {local ? "本地" : "系统继承"}
            </span>
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
          <span className="bg-muted rounded-full px-2 py-0.5">{team.nodes.length}</span>
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
                  <div className="text-muted-foreground truncate text-[10px]">
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
    </article>
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
    return <div className="space-y-3 py-12">{error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}<p className="text-muted-foreground text-sm">{error ?? "正在读取 Agent Team 配置…"}</p><Button variant="outline" onClick={() => void load()}>重新读取</Button></div>;
  }

  return (
    <div className="space-y-4">
      {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
      {saved && <div role="status" className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">配置更改成功</div>}
      <div className={`flex items-center gap-3 ${scope === "system" ? "justify-between" : "justify-end"}`}>
        {scope === "system" && <p className="text-muted-foreground text-sm">系统级 Team 配置</p>}
        <Button onClick={() => { setSaved(false); setEditing(null); }}><PlusIcon />新建 Team</Button>
      </div>
      {teams.length === 0 ? <div className="text-muted-foreground rounded-2xl border border-dashed p-8 text-center text-sm">还没有 Agent Team 配置</div> : <div className="grid gap-3">
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
