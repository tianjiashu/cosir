import type { Workspace, WorkspaceTask } from "@/lib/api/workspaces";

import { buildTaskTreeModel, type TaskTreeNode } from "./task-tree-model";

export type WorkspaceTreeSource = Workspace & {
  tasks: WorkspaceTask[];
  taskLoadError?: string;
};

export type WorkspaceSidebarNode =
  | {
      kind: "workspace";
      id: string;
      workspace: WorkspaceTreeSource;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "task";
      id: string;
      task: TaskTreeNode;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "placeholder";
      id: string;
      label: string;
      children: WorkspaceSidebarNode[];
    };

export function workspaceTreeNodeId(workspaceId: number): string {
  return `workspace:${workspaceId}`;
}

export function taskTreeNodeId(taskId: number): string {
  return `task:${taskId}`;
}

function flattenTasks(nodes: TaskTreeNode[]): TaskTreeNode[] {
  return nodes.flatMap((node) => [node, ...flattenTasks(node.children)]);
}

export function buildWorkspaceTreeModel(workspaces: WorkspaceTreeSource[]): WorkspaceSidebarNode[] {
  return workspaces.map((workspace) => {
    const tasks = flattenTasks(buildTaskTreeModel(workspace.tasks).roots);
    const children = tasks.length > 0
      ? tasks.map((task) => ({
          kind: "task" as const,
          id: taskTreeNodeId(task.task_id),
          task,
          children: [],
        }))
      : [{
          kind: "placeholder" as const,
          id: `workspace-empty:${workspace.workspace_id}`,
          label: workspace.taskLoadError ? "任务列表加载失败" : "暂无任务",
          children: [],
        }];
    return {
      kind: "workspace",
      id: workspaceTreeNodeId(workspace.workspace_id),
      workspace,
      children,
    };
  });
}
