import type { Workspace, WorkspaceTask } from "@/lib/api/workspaces";

import { buildTaskForest, type TaskTreeNode } from "./task-tree-model";

export type WorkspaceTreeSource = Workspace & {
  tasks: WorkspaceTask[];
  taskLoadError?: string;
};

export type WorkspaceSidebarNode =
  | {
      /** 跨工作区的置顶分组；没有置顶任务时不出现。 */
      kind: "pinned";
      id: string;
      children: WorkspaceSidebarNode[];
    }
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
      /** 只有顶层任务可置顶；Fork 随源任务一起移动，不单独置顶。 */
      pinnable: boolean;
      pinned: boolean;
      /** 仅置顶区的任务带上所属工作区名，供 tooltip 区分来源。 */
      workspaceName?: string;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "placeholder";
      id: string;
      label: string;
      /** 任务列表读取失败时为 true，行渲染据此使用失败语气而非空状态语气。 */
      isError: boolean;
      children: WorkspaceSidebarNode[];
    };

export type TaskSidebarNode = Extract<WorkspaceSidebarNode, { kind: "task" }>;

export function workspaceTreeNodeId(workspaceId: number): string {
  return `workspace:${workspaceId}`;
}

export function taskTreeNodeId(taskId: number): string {
  return `task:${taskId}`;
}

export const PINNED_GROUP_NODE_ID = "pinned";

/** Fork 作为源任务的子节点进入侧栏树，由 react-arborist 承载缩进、折叠与 `aria-level`。 */
function toForkSidebarNode(task: TaskTreeNode): WorkspaceSidebarNode {
  return {
    kind: "task",
    id: taskTreeNodeId(task.task_id),
    task,
    pinnable: false,
    pinned: false,
    children: task.children.map(toForkSidebarNode),
  };
}

function toRootSidebarNode(task: TaskTreeNode, pinned: boolean, workspaceName?: string): WorkspaceSidebarNode {
  return {
    kind: "task",
    id: taskTreeNodeId(task.task_id),
    task,
    pinnable: true,
    pinned,
    workspaceName,
    children: task.children.map(toForkSidebarNode),
  };
}

/**
 * 组装侧栏树：可选的置顶分组在最前，其后是各工作区。
 *
 * 置顶的顶层任务连同 Fork 子树整体移入置顶分组（按置顶顺序），保证每个任务在树中只出现一次，
 * 选中、`openParents` 与键盘导航不必区分两处。`pinnedTaskIds` 中找不到的 id 直接忽略。
 */
export function buildWorkspaceTreeModel(workspaces: WorkspaceTreeSource[], pinnedTaskIds: readonly number[]): WorkspaceSidebarNode[] {
  const pinnedOrder = new Map(pinnedTaskIds.map((taskId, index) => [taskId, index]));
  const pinned: { order: number; node: WorkspaceSidebarNode }[] = [];
  const workspaceNodes = workspaces.map((workspace): WorkspaceSidebarNode => {
    const children: WorkspaceSidebarNode[] = [];
    for (const task of buildTaskForest(workspace.tasks)) {
      const order = pinnedOrder.get(task.task_id);
      if (order === undefined) children.push(toRootSidebarNode(task, false));
      else pinned.push({ order, node: toRootSidebarNode(task, true, workspace.name) });
    }
    // 任务全部置顶时工作区下留空，不显示「暂无任务」：任务仍在，只是摆在置顶区。
    if (workspace.tasks.length === 0) {
      children.push({
        kind: "placeholder",
        id: `workspace-empty:${workspace.workspace_id}`,
        label: workspace.taskLoadError ? "任务列表加载失败" : "暂无任务",
        isError: Boolean(workspace.taskLoadError),
        children: [],
      });
    }
    return {
      kind: "workspace",
      id: workspaceTreeNodeId(workspace.workspace_id),
      workspace,
      children,
    };
  });
  if (pinned.length === 0) return workspaceNodes;
  pinned.sort((left, right) => left.order - right.order);
  return [{ kind: "pinned", id: PINNED_GROUP_NODE_ID, children: pinned.map((entry) => entry.node) }, ...workspaceNodes];
}
