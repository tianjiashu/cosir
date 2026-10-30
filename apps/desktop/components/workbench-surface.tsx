"use client";

import { lazy, Suspense, type ComponentType } from "react";

import { WorkbenchAgentRunSurface } from "@/components/workbench-agent-run-surface";
import { AgentConfigurationDraftSurface } from "@/components/agent-configuration-draft-surface";
import { AgentTeamConfigurationDraftSurface } from "@/components/agent-team-configuration-draft-surface";
import { TaskOverviewSurface } from "@/components/task-overview/task-overview-surface";
import type { Workspace } from "@/lib/api/workspaces";
import type { WorkbenchTab } from "@/lib/workbench/types";

const WorkspaceFilesSurface = lazy(() =>
  import("@/components/workspace-files/workspace-files-surface").then((module) => ({
    default: module.WorkspaceFilesSurface,
  })),
);

type SurfaceProps<K extends WorkbenchTab["kind"]> = {
  workspace: Workspace;
  tab: Extract<WorkbenchTab, { kind: K }>;
  onClose: () => void;
};

/**
 * Workbench 面板注册表：每种标签 kind 恰好对应一个面板，漏写或多写都在编译期报错。
 * 标签只由前端按 kind 创建，因此没有运行时的未知类型回退。
 */
const workbenchSurfaces: { [K in WorkbenchTab["kind"]]: ComponentType<SurfaceProps<K>> } = {
  files: ({ workspace }) => (
    <Suspense fallback={<div className="p-4 text-sm">加载文件浏览器…</div>}>
      <WorkspaceFilesSurface workspaceId={workspace.workspace_id} rootPath={workspace.root_path} workspaceName={workspace.name} />
    </Suspense>
  ),
  agent: ({ tab, onClose }) => <WorkbenchAgentRunSurface taskId={tab.taskId} onClose={onClose} />,
  "agent-configuration-draft": ({ workspace, tab, onClose }) => (
    <AgentConfigurationDraftSurface workspaceId={workspace.workspace_id} tab={tab} onClose={onClose} />
  ),
  "agent-team-configuration-draft": ({ workspace, tab, onClose }) => (
    <AgentTeamConfigurationDraftSurface workspaceId={workspace.workspace_id} tab={tab} onClose={onClose} />
  ),
  "task-overview": () => <TaskOverviewSurface />,
};

export function WorkbenchSurface({ workspace, tab, onClose }: SurfaceProps<WorkbenchTab["kind"]>) {
  const Surface = workbenchSurfaces[tab.kind] as ComponentType<SurfaceProps<WorkbenchTab["kind"]>>;
  return <Surface workspace={workspace} tab={tab} onClose={onClose} />;
}
