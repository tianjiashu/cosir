"use client";

import type { ComponentType } from "react";
import { WorkbenchAgentRunSurface } from "@/components/workbench-agent-run-surface";
import type { WorkbenchTab } from "@/lib/workbench/types";

export type WorkbenchSurfaceKind = "agent" | "terminal" | "web" | "file-diff";
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

/**
 * Workbench surface registry. New surfaces register a renderer here while
 * keeping tab lifecycle, active-only mounting, and unknown-kind fallback in
 * the shared Workbench shell.
 */
export const workbenchSurfaceRegistry: Partial<Record<WorkbenchSurfaceKind, RegisteredSurface>> = {
  agent: { render: agentSurface },
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
