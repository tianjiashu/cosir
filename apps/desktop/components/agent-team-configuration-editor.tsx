"use client";

import { useState } from "react";
import { AgentTeamConfigurationForm } from "@/components/agent-team-configuration-form";

import { Button } from "@/components/ui/button";
import { AgentTeamGraphEditor } from "@/components/agent-team-graph-editor";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

const emptyConfiguration = (scope: AgentTeamConfiguration["scope"]): AgentTeamConfiguration => ({
  team_id: "",
  name: "",
  description: "",
  max_runs: 10,
  start_node_id: "",
  nodes: [],
  transitions: [],
  scope,
});

/** 统一协调互斥的表单与画布视图，并持有共享配置、错误和保存状态。 */
export function AgentTeamConfigurationEditor({
  initial,
  profilesByScope,
  scopeEditable,
  defaultScope = "workspace",
  graphLayoutKey,
  onChange,
  onCancel,
  onSave,
}: {
  initial: AgentTeamConfiguration | null;
  profilesByScope: AgentProfilesByScope;
  scopeEditable: boolean;
  defaultScope?: AgentTeamConfiguration["scope"];
  graphLayoutKey?: (configuration: AgentTeamConfiguration) => string;
  onChange?: (configuration: AgentTeamConfiguration) => void;
  onCancel: () => void;
  onSave: (configuration: AgentTeamConfiguration) => Promise<void>;
}) {
  const [configuration, setConfiguration] = useState<AgentTeamConfiguration>(() => initial ? { ...initial } : emptyConfiguration(defaultScope));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editorMode, setEditorMode] = useState<"form" | "canvas">("form");
  const [canvasVisited, setCanvasVisited] = useState(false);
  const graphLayoutKeyFor = graphLayoutKey ?? ((value: AgentTeamConfiguration) => `cosir:agent-team-graph:${value.scope}:${value.team_id || "new"}`);
  const [resolvedGraphLayoutKey] = useState(() => graphLayoutKeyFor(initial ?? emptyConfiguration(defaultScope)));
  const profiles = profilesByScope[configuration.scope].filter((profile) => profile.validation_status === "valid");

  const applyConfiguration = (next: AgentTeamConfiguration) => {
    setConfiguration(next);
    onChange?.(next);
    setError(null);
  };

  const save = async () => {
    if (!configuration.team_id.trim() || !/^[A-Za-z0-9_-]+$/.test(configuration.team_id)) {
      setError("Team ID 只能包含字母、数字、下划线和连字符");
      return;
    }
    if (!configuration.name.trim() || !configuration.description.trim()) {
      setError("请填写 Team 名称和用途说明");
      return;
    }
    if (!Number.isInteger(configuration.max_runs) || configuration.max_runs < 1) {
      setError("最大轮数必须是正整数");
      return;
    }
    setSaving(true);
    setError(null);
    const savedConfiguration = { ...configuration, team_id: configuration.team_id.trim() };
    try {
      await onSave(savedConfiguration);
      const savedGraphLayoutKey = graphLayoutKeyFor(savedConfiguration);
      if (savedGraphLayoutKey !== resolvedGraphLayoutKey) {
        try {
          const layout = window.localStorage.getItem(resolvedGraphLayoutKey);
          if (layout !== null) {
            window.localStorage.setItem(savedGraphLayoutKey, layout);
            window.localStorage.removeItem(resolvedGraphLayoutKey);
          }
        } catch (cause) {
          void frontendLog("WARNING", "agent_team_graph_layout_migration_failed", "保存 Team 后迁移画布布局失败", {
            traceId: newTraceId(),
            data: { teamId: savedConfiguration.team_id },
            error: cause,
          });
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="border-border/70 bg-card/80 rounded-2xl border shadow-sm">
      <header className="flex items-start justify-between gap-3 border-b px-5 py-4">
        <h2 className="font-medium">{initial ? "编辑 Agent Team" : "新建 Agent Team"}</h2>
        <div className="flex shrink-0 items-center gap-2">
          <div className="bg-muted relative isolate grid h-10 w-40 grid-cols-2 rounded-lg p-1" role="group" aria-label="编辑模式">
            <span
              aria-hidden="true"
              className={`absolute inset-y-1 left-1 w-[calc(50%-0.25rem)] rounded-md bg-blue-600 shadow-sm transition-transform duration-200 ${editorMode === "canvas" ? "translate-x-full" : "translate-x-0"}`}
            />
            <button
              type="button"
              aria-pressed={editorMode === "form"}
              className={`relative z-10 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 ${editorMode === "form" ? "text-white" : "text-muted-foreground hover:text-foreground"}`}
              onClick={() => setEditorMode("form")}
            >表单</button>
            <button
              type="button"
              aria-pressed={editorMode === "canvas"}
              className={`relative z-10 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 ${editorMode === "canvas" ? "text-white" : "text-muted-foreground hover:text-foreground"}`}
              onClick={() => { setCanvasVisited(true); setEditorMode("canvas"); }}
            >画布</button>
          </div>
          <Button type="button" variant="outline" onClick={onCancel}>返回 Team 列表</Button>
        </div>
      </header>
      <div className={editorMode === "form" ? "block" : "hidden"}>
        <AgentTeamConfigurationForm
          configuration={configuration}
          profiles={profiles}
          scopeEditable={scopeEditable}
          teamIdEditable={!initial}
          error={error}
          saving={saving}
          onChange={applyConfiguration}
          onError={setError}
          onSave={save}
          onCancel={onCancel}
        />
      </div>
      {canvasVisited && <div className={editorMode === "canvas" ? "p-5" : "hidden"}>
        <AgentTeamGraphEditor
          configuration={configuration}
          profiles={profiles}
          storageKey={resolvedGraphLayoutKey}
          onSave={() => void save()}
          onExit={() => setEditorMode("form")}
          error={error}
          saving={saving}
          scopeEditable={scopeEditable}
          teamIdEditable={!initial}
          onChange={applyConfiguration}
        />
      </div>}
    </section>
  );
}
