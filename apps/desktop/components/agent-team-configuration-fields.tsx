"use client";

import { Input } from "@/components/ui/input";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";

export function AgentTeamConfigurationFields({
  configuration,
  onChange,
  teamIdEditable,
  scopeEditable,
  layout,
}: {
  configuration: AgentTeamConfiguration;
  onChange: (changes: Partial<AgentTeamConfiguration>, historyKey?: string) => void;
  teamIdEditable: boolean;
  scopeEditable: boolean;
  layout: "form" | "dialog";
}) {
  const labelClassName = layout === "form" ? "space-y-1.5 text-sm" : "block space-y-1 text-xs";
  const labelTextClassName = "text-muted-foreground";

  return (
    <div className={layout === "form" ? "space-y-4" : "space-y-3"}>
      {scopeEditable && <label className={`block ${layout === "form" ? "space-y-1.5 text-sm" : "space-y-1 text-xs"}`}>
        <span className={labelTextClassName}>保存范围</span>
        <select
          className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm"
          value={configuration.scope}
          onChange={(event) => onChange({ scope: event.target.value as AgentTeamConfiguration["scope"] }, "team-scope")}
        >
          <option value="workspace">当前工作区</option>
          <option value="system">系统级</option>
        </select>
      </label>}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={labelClassName}>
          <span className={labelTextClassName}>Team ID</span>
          <Input
            value={configuration.team_id}
            disabled={!teamIdEditable}
            onChange={(event) => onChange({ team_id: event.target.value }, "team-id")}
            placeholder="例如 code_review_team"
          />
        </label>
        <label className={labelClassName}>
          <span className={labelTextClassName}>Team 名称</span>
          <Input value={configuration.name} onChange={(event) => onChange({ name: event.target.value }, "team-name")} />
        </label>
      </div>
      <label className={`block ${labelClassName}`}>
        <span className={labelTextClassName}>用途说明</span>
        <Input value={configuration.description} onChange={(event) => onChange({ description: event.target.value }, "team-description")} />
      </label>
      <label className={`block ${labelClassName}`}>
        <span className={labelTextClassName}>最大轮数</span>
        <Input
          type="number"
          min={1}
          value={configuration.max_runs}
          onChange={(event) => onChange({ max_runs: Number(event.target.value) }, "team-max-runs")}
        />
      </label>
    </div>
  );
}
