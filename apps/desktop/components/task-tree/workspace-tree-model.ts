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
    }
  | {
      /** 工作区末尾的「显示更多 / 收起」行；`hiddenCount` 为 0 时是「收起」。 */
      kind: "more";
      id: string;
      workspaceId: number;
      hiddenCount: number;
      children: WorkspaceSidebarNode[];
    };

export type TaskSidebarNode = Extract<WorkspaceSidebarNode, { kind: "task" }>;

export function workspaceTreeNodeId(workspaceId: number): string {
  return `workspace:${workspaceId}`;
}

export function taskTreeNodeId(taskId: number): string {
  return `task:${taskId}`;
}

const PINNED_GROUP_NODE_ID = "pinned";

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

/** 每个工作区默认显示的顶层任务数，也是「显示更多」每次多显示的条数。 */
export const TASK_PAGE_SIZE = 5;

/**
 * 组装侧栏树时的展示状态：各工作区的水位线（显示前多少个顶层任务）与必须可见的任务。
 *
 * 水位线由 WorkspaceTree 持有、不持久化；缺省为 `TASK_PAGE_SIZE`。
 */
export type TaskVisibility = {
  watermarks: ReadonlyMap<number, number>;
  /** 当前打开、运行中或等待中的任务：所在的顶层任务排在水位线之后时，把水位线推到它。 */
  mustShowTaskIds: ReadonlySet<number>;
};

export type WorkspaceTreeModel = {
  nodes: WorkspaceSidebarNode[];
  /** 本次实际使用的水位线（已含必须可见任务的推高）；树据此只升不降地记下，任务停止运行后不会消失。 */
  watermarks: Map<number, number>;
};

function subtreeContains(task: TaskTreeNode, taskIds: ReadonlySet<number>): boolean {
  return taskIds.has(task.task_id) || task.children.some((child) => subtreeContains(child, taskIds));
}

/** 水位线：默认值与「最后一个必须可见的顶层任务」之间取较大者，按顶层任务计数（Fork 跟随源任务）。 */
function resolveWatermark(roots: TaskTreeNode[], stored: number | undefined, mustShow: ReadonlySet<number>): number {
  let watermark = stored ?? TASK_PAGE_SIZE;
  roots.forEach((root, index) => {
    if (index >= watermark && subtreeContains(root, mustShow)) watermark = index + 1;
  });
  return watermark;
}

/**
 * 组装侧栏树：可选的置顶分组在最前，其后是各工作区。
 *
 * 置顶的顶层任务连同 Fork 子树整体移入置顶分组（按置顶顺序），保证每个任务在树中只出现一次，
 * 选中、`openParents` 与键盘导航不必区分两处。`pinnedTaskIds` 中找不到的 id 直接忽略。
 * 置顶区不受水位线限制；工作区只显示水位线以内的顶层任务，其余收进末尾的「显示更多」行，
 * 全部显示且超过默认条数时末尾是「收起」。
 */
export function buildWorkspaceTreeModel(
  workspaces: WorkspaceTreeSource[],
  pinnedTaskIds: readonly number[],
  visibility: TaskVisibility,
): WorkspaceTreeModel {
  const pinnedOrder = new Map(pinnedTaskIds.map((taskId, index) => [taskId, index]));
  const pinned: { order: number; node: WorkspaceSidebarNode }[] = [];
  const watermarks = new Map<number, number>();
  const workspaceNodes = workspaces.map((workspace): WorkspaceSidebarNode => {
    const roots: TaskTreeNode[] = [];
    for (const task of buildTaskForest(workspace.tasks)) {
      const order = pinnedOrder.get(task.task_id);
      if (order === undefined) roots.push(task);
      else pinned.push({ order, node: toRootSidebarNode(task, true, workspace.name) });
    }
    const watermark = resolveWatermark(roots, visibility.watermarks.get(workspace.workspace_id), visibility.mustShowTaskIds);
    watermarks.set(workspace.workspace_id, watermark);
    const children = roots.slice(0, watermark).map((task) => toRootSidebarNode(task, false));
    const hiddenCount = Math.max(0, roots.length - watermark);
    if (hiddenCount > 0 || roots.length > TASK_PAGE_SIZE) {
      children.push({ kind: "more", id: `workspace-more:${workspace.workspace_id}`, workspaceId: workspace.workspace_id, hiddenCount, children: [] });
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
  if (pinned.length === 0) return { nodes: workspaceNodes, watermarks };
  pinned.sort((left, right) => left.order - right.order);
  return {
    nodes: [{ kind: "pinned", id: PINNED_GROUP_NODE_ID, children: pinned.map((entry) => entry.node) }, ...workspaceNodes],
    watermarks,
  };
}
