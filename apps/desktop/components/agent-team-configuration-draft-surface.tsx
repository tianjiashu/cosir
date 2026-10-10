"use client";

import { useEffect, useState } from "react";
import { UsersIcon, XIcon } from "lucide-react";

import { AgentTeamConfigurationEditor } from "@/components/agent-team-configuration-editor";
import { Button } from "@/components/ui/button";
import {
  createAgentTeamConfiguration,
  updateAgentTeamConfiguration,
  type AgentTeamConfiguration,
} from "@/lib/api/agent-teams";
import {
  getAgentConfigurations,
  getWorkspaceAgentConfigurations,
  type AgentConfiguration,
} from "@/lib/api/configuration";
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchAgentTeamConfigurationDraftTab } from "@/lib/workbench/types";

type ProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

function visibleWorkspaceProfiles(system: AgentConfiguration[], workspace: AgentConfiguration[]) {
  return [...new Map([...system, ...workspace].map((profile) => [profile.agent_id, profile])).values()];
}

/** 在 Workbench 中编辑并显式保存一次工具生成的 Team 配置草稿。 */
export function AgentTeamConfigurationDraftSurface({
  tab,
  onClose,
}: {
  tab: WorkbenchAgentTeamConfigurationDraftTab;
  onClose: () => void;
}) {
  const updateDraft = useWorkbenchStore((state) => state.updateAgentTeamConfigurationDraft);
  const setDirty = useWorkbenchStore((state) => state.setAgentTeamDraftDirty);
  const markSaved = useWorkbenchStore((state) => state.markAgentTeamDraftSaved);
  const [profiles, setProfiles] = useState<ProfilesByScope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let active = true;
    void Promise.all([
      getAgentConfigurations(),
      getWorkspaceAgentConfigurations(tab.workspaceId),
    ]).then(([systemAgents, workspaceAgents]) => {
      if (!active) return;
      setProfiles({
        system: systemAgents,
        workspace: visibleWorkspaceProfiles(systemAgents, workspaceAgents),
      });
      setError(null);
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : "读取子 Agent 配置失败");
    });
    return () => { active = false; };
  }, [tab.workspaceId]);

  const close = () => {
    if (tab.dirty && !window.confirm("Agent Team 配置草稿尚未保存，确定关闭吗？")) return;
    onClose();
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-2"><UsersIcon className="size-4 shrink-0" /><div className="min-w-0"><h2 className="truncate text-sm font-medium">Agent Team 配置草稿</h2><p className="text-muted-foreground truncate text-xs">对话生成，仅保存后生效</p></div></div>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭 Team 配置草稿" onClick={close}><XIcon /></Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error && <div role="alert" className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
        {saved && <div role="status" className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">配置已保存</div>}
        {!profiles ? <p className="text-muted-foreground py-12 text-sm">{error ?? "正在读取子 Agent 配置…"}</p> : <AgentTeamConfigurationEditor
          key={tab.id}
          initial={tab.draft}
          profilesByScope={profiles}
          scopeEditable={tab.savedScope === null}
          graphLayoutKey={`cosir:agent-team-graph:draft:${tab.id}:${tab.draft.scope}:${tab.draft.team_id || "new"}`}
          onChange={(configuration) => {
            updateDraft(tab.id, configuration);
            setDirty(tab.id, true);
            setSaved(false);
          }}
          onCancel={close}
          onSave={async (configuration) => {
            const input = { scope: configuration.scope, configuration } as const;
            const workspaceId = configuration.scope === "workspace" ? tab.workspaceId : undefined;
            if (tab.savedScope === null) {
              await createAgentTeamConfiguration(input, workspaceId);
            } else {
              if (configuration.scope !== tab.savedScope) {
                throw new Error("已保存的草稿不能更改保存范围");
              }
              await updateAgentTeamConfiguration(configuration.team_id, input, workspaceId);
            }
            updateDraft(tab.id, configuration);
            markSaved(tab.id, configuration.scope);
            setSaved(true);
          }}
        />}
      </div>
    </section>
  );
}
