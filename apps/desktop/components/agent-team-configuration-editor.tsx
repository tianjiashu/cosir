"use client";

import { useState } from "react";
import { AgentTeamConfigurationForm } from "@/components/agent-team-configuration-form";

import { Button } from "@/components/ui/button";
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { AgentTeamGraphEditor } from "@/components/agent-team-graph-editor";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration, AgentTeamConfigurationDraft } from "@/lib/api/agent-teams";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import { cn } from "@/lib/utils";

type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

const EDITOR_MODE_OPTIONS = [
  { value: "form", label: "表单" },
  { value: "canvas", label: "画布" },
] as const;

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
    <Card className={cn(editorMode === "canvas" && "min-w-0 rounded-none border-0 bg-transparent shadow-none")}>
      <CardHeader>
        <CardTitle>{initial ? "编辑 Agent Team" : "新建 Agent Team"}</CardTitle>
        <CardAction>
          <SegmentedControl
            aria-label="编辑模式"
            className="w-40"
            value={editorMode}
            options={EDITOR_MODE_OPTIONS}
            onValueChange={(mode) => {
              // 画布首次进入时才挂载，之后保持挂载以保留视口与撤销历史。
              if (mode === "canvas") setCanvasVisited(true);
              setEditorMode(mode);
            }}
          />
          <Button type="button" variant="outline" onClick={onCancel}>返回 Team 列表</Button>
        </CardAction>
      </CardHeader>
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
    </Card>
  );
}
