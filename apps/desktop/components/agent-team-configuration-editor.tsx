"use client";

import { useState } from "react";
import { AgentTeamConfigurationForm } from "@/components/agent-team-configuration-form";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { AgentTeamGraphEditor } from "@/components/agent-team-graph-editor";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration, AgentTeamConfigurationDraft } from "@/lib/api/agent-teams";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

const emptyConfiguration = (scope: AgentTeamConfiguration["scope"]): AgentTeamConfigurationDraft => ({
  team_id: "",
  name: "",
  description: "",
  max_runs: 10,
  start_node_id: "",
  nodes: [],
  transitions: [],
  scope,
});

/** 统一协调表单与画布编辑，并把保存结果作为后端校验反馈呈现给用户。 */
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
  initial: AgentTeamConfiguration | AgentTeamConfigurationDraft | null;
  profilesByScope: AgentProfilesByScope;
  scopeEditable: boolean;
  defaultScope?: AgentTeamConfiguration["scope"];
  graphLayoutKey?: (configuration: AgentTeamConfigurationDraft) => string;
  onChange?: (configuration: AgentTeamConfigurationDraft) => void;
  onCancel: () => void;
  onSave: (configuration: AgentTeamConfigurationDraft) => Promise<void>;
}) {
  const [configuration, setConfiguration] = useState<AgentTeamConfigurationDraft>(() => initial ? { ...initial } : emptyConfiguration(defaultScope));
  const [saving, setSaving] = useState(false);
  const [saveFeedback, setSaveFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);
  const [editorMode, setEditorMode] = useState<"form" | "canvas">("form");
  const [canvasVisited, setCanvasVisited] = useState(false);
  const graphLayoutKeyFor = graphLayoutKey ?? ((value: AgentTeamConfigurationDraft) => `cosir:agent-team-graph:${value.scope}:${value.team_id || "new"}`);
  const [resolvedGraphLayoutKey, setResolvedGraphLayoutKey] = useState(() => graphLayoutKeyFor(initial ?? emptyConfiguration(defaultScope)));
  const profiles = profilesByScope[configuration.scope];

  const applyConfiguration = (next: AgentTeamConfigurationDraft) => {
    setConfiguration(next);
    onChange?.(next);
  };

  const save = async () => {
    setSaving(true);
    setSaveFeedback(null);
    const savedConfiguration = { ...configuration };
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
          setResolvedGraphLayoutKey(savedGraphLayoutKey);
        } catch (cause) {
          void frontendLog("WARNING", "agent_team_graph_layout_migration_failed", "保存 Team 后迁移画布布局失败", {
            traceId: newTraceId(),
            data: { teamId: savedConfiguration.team_id },
            error: cause,
          });
        }
      }
      setSaveFeedback({ kind: "success", message: "Agent Team 配置已保存。" });
    } catch (cause) {
      setSaveFeedback({
        kind: "error",
        message: cause instanceof Error && cause.message ? cause.message : "保存失败，请检查配置后重试。",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className={editorMode === "canvas" ? "min-w-0" : "border-border/70 bg-card/80 rounded-2xl border shadow-sm"}>
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
          saving={saving}
          onChange={applyConfiguration}
          onSave={save}
          onCancel={onCancel}
        />
      </div>
      {canvasVisited && <div className={editorMode === "canvas" ? "min-w-0" : "hidden"}>
        <AgentTeamGraphEditor
          configuration={configuration}
          profiles={profiles}
          storageKey={resolvedGraphLayoutKey}
          onSave={() => void save()}
          onExit={() => setEditorMode("form")}
          saving={saving}
          scopeEditable={scopeEditable}
          teamIdEditable={!initial}
          onChange={applyConfiguration}
        />
      </div>}
      <Dialog open={saveFeedback !== null} onOpenChange={(open) => { if (!open) setSaveFeedback(null); }}>
        <DialogContent showCloseButton={false}>
          <DialogTitle>{saveFeedback?.kind === "success" ? "保存成功" : "保存失败"}</DialogTitle>
          <DialogDescription className={saveFeedback?.kind === "error" ? "whitespace-pre-wrap text-destructive" : undefined}>
            {saveFeedback?.message}
          </DialogDescription>
          <DialogFooter>
            <Button type="button" onClick={() => setSaveFeedback(null)}>
              {saveFeedback?.kind === "success" ? "继续编辑" : "返回修改"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
