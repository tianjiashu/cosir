"use client";

import type { ComponentType } from "react";
import { WorkbenchAgentRunSurface } from "@/components/workbench-agent-run-surface";
import { AgentConfigurationDraftSurface } from "@/components/agent-configuration-draft-surface";
import { AgentTeamConfigurationDraftSurface } from "@/components/agent-team-configuration-draft-surface";
import type { WorkbenchTab } from "@/lib/workbench/types";

export type WorkbenchSurfaceKind = "agent" | "agent-configuration-draft" | "agent-team-configuration-draft" | "terminal" | "web" | "file-diff";
export type WorkbenchSurfaceProps = {
  tab: WorkbenchTab;
  onClose: () => void;
};

type RegisteredSurface = {
  render: ComponentType<WorkbenchSurfaceProps>;
};

const agentSurface: ComponentType<WorkbenchSurfaceProps> = ({ tab, onClose }) => {
  if (tab.kind !== "agent") return <UnknownWorkbenchSurface kind={tab.kind} />;
  return <WorkbenchAgentRunSurface taskId={tab.taskId} onClose={onClose} />;
};

const agentConfigurationDraftSurface: ComponentType<WorkbenchSurfaceProps> = ({ tab, onClose }) => {
  if (tab.kind !== "agent-configuration-draft") return <UnknownWorkbenchSurface kind={tab.kind} />;
  return <AgentConfigurationDraftSurface tab={tab} onClose={onClose} />;
};

const agentTeamConfigurationDraftSurface: ComponentType<WorkbenchSurfaceProps> = ({ tab, onClose }) => {
  if (tab.kind !== "agent-team-configuration-draft") return <UnknownWorkbenchSurface kind={tab.kind} />;
  return <AgentTeamConfigurationDraftSurface tab={tab} onClose={onClose} />;
};

/** Workbench 面板渲染注册表；标签生命周期、按需挂载和未知类型回退由共享容器统一管理。 */
export const workbenchSurfaceRegistry: Partial<Record<WorkbenchSurfaceKind, RegisteredSurface>> = {
  agent: { render: agentSurface },
  "agent-configuration-draft": { render: agentConfigurationDraftSurface },
  "agent-team-configuration-draft": { render: agentTeamConfigurationDraftSurface },
};

function UnknownWorkbenchSurface({ kind }: { kind: string }) {
  return <div className="text-muted-foreground flex h-full items-center justify-center p-6 text-sm">暂不支持的工作台类型：{kind}</div>;
}

export function WorkbenchSurface({ tab, onClose }: WorkbenchSurfaceProps) {
  const surface = workbenchSurfaceRegistry[tab.kind as WorkbenchSurfaceKind];
  if (!surface) return <UnknownWorkbenchSurface kind={tab.kind} />;
  const Renderer = surface.render;
  return <Renderer tab={tab} onClose={onClose} />;
}
