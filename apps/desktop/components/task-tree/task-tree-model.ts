import type { WorkspaceTask } from "@/lib/api/workspaces";
import { stripInlineAttachmentTokens } from "@/lib/assistant/attachments/local-file-token";

export type TaskTreeNode = WorkspaceTask & {
  children: TaskTreeNode[];
  depth: number;
  branchIndex: number | null;
  /** 侧栏标题：已剥离内部 token、不截断；按侧栏宽度由 CSS 省略，完整文本在 tooltip 中可见。 */
  full_title: string;
};

export type TaskTreeModel = {
  roots: TaskTreeNode[];
  nodesById: Map<number, TaskTreeNode>;
  parentById: Map<number, number>;
};

/**
 * Return the direct Fork source encoded by the backend response.
 *
 * This is intentionally limited to Fork tasks. Delegation relationships are
 * not part of the workspace sidebar and must not accidentally enter this tree.
 */
function getForkSourceTaskId(task: WorkspaceTask): number | null {
  if (task.task_type !== "fork") return null;
  const sourceTaskId = task.extra?.fork?.source_task_id;
  return typeof sourceTaskId === "number" && Number.isInteger(sourceTaskId) ? sourceTaskId : null;
}

/** 标题完全由内部 token 组成时的展示回退文案。 */
const EMPTY_TITLE_FALLBACK = "新对话";

/**
 * 把原始标题归一为用户可读文本。
 *
 * 负责什么：剥离内联附件 token（任务标题由首条消息原文派生，可能含 `[[cosir-file:<id>]]`），
 * 全为 token 时回退为占位文案。
 * 不负责什么：不截断长度、不修改任务事实。
 *
 * 参数:
 *     rawTitle: 后端返回的任务标题原文。
 *
 * 返回:
 *     剥离 token 后的单行标题；无可见内容时返回 `EMPTY_TITLE_FALLBACK`。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
function visibleTaskTitle(rawTitle: string): string {
  return stripInlineAttachmentTokens(rawTitle) || EMPTY_TITLE_FALLBACK;
}

function compareByUpdatedAt(left: WorkspaceTask, right: WorkspaceTask): number {
  const updatedDifference = right.updated_at.localeCompare(left.updated_at);
  return updatedDifference || right.task_id - left.task_id;
}

function compareByCreatedAt(left: WorkspaceTask, right: WorkspaceTask): number {
  const createdDifference = left.created_at.localeCompare(right.created_at);
  return createdDifference || left.task_id - right.task_id;
}

function hasParentCycle(taskId: number, parentCandidates: Map<number, number>): boolean {
  const visited = new Set<number>();
  let currentId: number | undefined = taskId;
  while (currentId !== undefined) {
    if (visited.has(currentId)) return true;
    visited.add(currentId);
    currentId = parentCandidates.get(currentId);
  }
  return false;
}

/**
 * Convert the flat workspace catalog into a defensive Fork-only forest.
 *
 * Valid Forks are attached to their source task recursively. A missing source
 * or a cycle is treated as a root so malformed persisted metadata cannot hide
 * a task or cause recursive rendering to loop forever. Roots are ordered by
 * recent activity; siblings are ordered by creation time to keep a branch's
 * position stable while it is being used.
 */
export function buildTaskTreeModel(tasks: WorkspaceTask[]): TaskTreeModel {
  const forkTasks = tasks.filter((task) => task.task_type !== "delegation");
  const taskById = new Map(forkTasks.map((task) => [task.task_id, task]));
  const parentCandidates = new Map<number, number>();

  for (const task of forkTasks) {
    const sourceTaskId = getForkSourceTaskId(task);
    if (sourceTaskId !== null && sourceTaskId !== task.task_id && taskById.has(sourceTaskId)) {
      parentCandidates.set(task.task_id, sourceTaskId);
    }
  }

  const parentById = new Map<number, number>();
  for (const [taskId, sourceTaskId] of parentCandidates) {
    if (!hasParentCycle(taskId, parentCandidates)) parentById.set(taskId, sourceTaskId);
  }

  const childrenByParent = new Map<number, WorkspaceTask[]>();
  for (const [taskId, parentId] of parentById) {
    const task = taskById.get(taskId);
    if (!task) continue;
    const siblings = childrenByParent.get(parentId) ?? [];
    siblings.push(task);
    childrenByParent.set(parentId, siblings);
  }
  for (const siblings of childrenByParent.values()) siblings.sort(compareByCreatedAt);

  const nodesById = new Map<number, TaskTreeNode>();
  const makeNode = (task: WorkspaceTask, depth: number, branchIndex: number | null): TaskTreeNode => {
    const fullTitle = visibleTaskTitle(task.title);
    const node: TaskTreeNode = {
      ...task,
      children: [],
      depth,
      branchIndex,
      full_title: fullTitle,
    };
    nodesById.set(task.task_id, node);
    const children = childrenByParent.get(task.task_id) ?? [];
    node.children = children.map((child, index) => makeNode(child, depth + 1, index + 1));
    return node;
  };

  const roots = forkTasks
    .filter((task) => !parentById.has(task.task_id))
    .sort(compareByUpdatedAt)
    .map((task) => makeNode(task, 0, null));

  return { roots, nodesById, parentById };
}

export function getTaskAncestorIds(taskId: number | null, parentById: Map<number, number>): Set<number> {
  const ancestors = new Set<number>();
  let currentId = taskId === null ? undefined : parentById.get(taskId);
  while (currentId !== undefined && !ancestors.has(currentId)) {
    ancestors.add(currentId);
    currentId = parentById.get(currentId);
  }
  return ancestors;
}
