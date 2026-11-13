import type { ComponentType } from "react";

import { WorkbenchAgentRunSurface } from "./agent-run-surface";
import { AgentConfigurationDraftSurface } from "./agent-configuration-draft-surface";
import type { Workspace } from "@/lib/api/workspaces";
import type { WorkbenchTab } from "@/lib/workbench/workbench-tab-types";

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
  agent: ({ workspace, tab, onClose }) => (
    <WorkbenchAgentRunSurface taskId={tab.taskId} workspaceId={workspace.workspace_id} onClose={onClose} />
  ),
  "agent-configuration-draft": ({ workspace, tab, onClose }) => (
    <AgentConfigurationDraftSurface workspaceId={workspace.workspace_id} tab={tab} onClose={onClose} />
  ),
};

export function WorkbenchSurface({ workspace, tab, onClose }: SurfaceProps<WorkbenchTab["kind"]>) {
  const Surface = workbenchSurfaces[tab.kind] as ComponentType<SurfaceProps<WorkbenchTab["kind"]>>;
  return <Surface workspace={workspace} tab={tab} onClose={onClose} />;
}
