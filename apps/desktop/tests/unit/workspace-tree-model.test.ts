import { describe, expect, it } from "vitest";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import {
  buildWorkspaceTreeModel as build,
  TASK_PAGE_SIZE,
  type TaskVisibility,
  type WorkspaceSidebarNode,
  type WorkspaceTreeSource,
} from "@/components/task-tree/workspace-tree-model";

/** 任务 id 越大越新：顶层任务按最近更新倒序排列。 */
function task(taskId: number, workspaceId: number, sourceTaskId?: number): WorkspaceTask {
  const at = new Date(Date.UTC(2026, 0, 1, 0, taskId)).toISOString();
  return {
    task_id: taskId,
    workspace_id: workspaceId,
    title: `Task ${taskId}`,
    task_type: sourceTaskId ? "user_fork" : "user",
    extra: sourceTaskId ? { fork: { tree_parent_task_id: sourceTaskId } } : null,
    created_at: at,
    updated_at: at,
  };
}

const noVisibility: TaskVisibility = { watermarks: new Map(), mustShowTaskIds: new Set() };

function buildWorkspaceTreeModel(workspaces: WorkspaceTreeSource[], pinnedTaskIds: readonly number[], visibility = noVisibility) {
  return build(workspaces, pinnedTaskIds, visibility).nodes;
}

/** 编号 1..count 的顶层任务；按最近更新排序后显示顺序是 count..1。 */
function tasks(count: number): WorkspaceTask[] {
  return Array.from({ length: count }, (_, index) => task(index + 1, 1));
}

function workspace(workspaceId: number, tasks: WorkspaceTask[]): WorkspaceTreeSource {
  const at = "2026-01-01T00:00:00.000Z";
  return { workspace_id: workspaceId, name: `ws-${workspaceId}`, root_path: `/ws/${workspaceId}`, created_at: at, updated_at: at, tasks };
}

const ids = (nodes: WorkspaceSidebarNode[]) => nodes.map((node) => node.id);

describe("buildWorkspaceTreeModel", () => {
  it("omits the pinned group when nothing is pinned and ignores unknown ids", () => {
    const model = buildWorkspaceTreeModel([workspace(1, [task(1, 1)])], [99]);
    expect(ids(model)).toEqual(["workspace:1"]);
  });

  it("moves pinned root tasks with their Fork subtree across workspaces in pin order", () => {
    const model = buildWorkspaceTreeModel(
      [workspace(1, [task(1, 1), task(2, 1, 1), task(3, 1)]), workspace(2, [task(4, 2)])],
      [4, 1, 2],
    );
    expect(ids(model)).toEqual(["pinned", "workspace:1", "workspace:2"]);
    const [pinned, first, second] = model;
    // Fork 2 不是顶层任务，不能单独置顶，随源任务 1 一起移动。
    expect(ids(pinned.children)).toEqual(["task:4", "task:1"]);
    expect(ids(pinned.children[1].children)).toEqual(["task:2"]);
    expect(pinned.children[0]).toMatchObject({ pinned: true, pinnable: true, workspaceName: "ws-2" });
    expect(pinned.children[1].children[0]).toMatchObject({ pinned: false, pinnable: false });
    expect(ids(first.children)).toEqual(["task:3"]);
    // 任务都在置顶区时工作区下留空，不显示「暂无任务」。
    expect(second.children).toEqual([]);
  });

  it("keeps the empty placeholder for workspaces without tasks", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, [])], []);
    expect(node.children).toMatchObject([{ kind: "placeholder", label: "暂无任务" }]);
  });

  it("shows the newest page of root tasks and summarizes the rest in a more row", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, tasks(8))], []);
    expect(ids(node.children)).toEqual(["task:8", "task:7", "task:6", "task:5", "task:4", "workspace-more:1"]);
    expect(node.children.at(-1)).toMatchObject({ kind: "more", workspaceId: 1, hiddenCount: 3 });
  });

  it("adds no more row when every root fits in the first page", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, tasks(TASK_PAGE_SIZE))], []);
    expect(node.children.map((child) => child.kind)).toEqual(Array(TASK_PAGE_SIZE).fill("task"));
  });

  it("counts root tasks only: a Fork rides along with its source", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, [...tasks(6), task(9, 1, 2)])], []);
    expect(ids(node.children)).toEqual(["task:6", "task:5", "task:4", "task:3", "task:2", "workspace-more:1"]);
    expect(ids(node.children[4].children)).toEqual(["task:9"]);
  });

  it("raises the watermark to the last must-show root, including one reached through a Fork", () => {
    const model = build([workspace(1, [...tasks(10), task(11, 1, 2)])], [], { watermarks: new Map(), mustShowTaskIds: new Set([11]) });
    // 任务 2 是倒数第二个顶层任务（第 9 位），它的 Fork 11 必须可见。
    expect(model.watermarks.get(1)).toBe(9);
    expect(model.nodes[0].children.at(-1)).toMatchObject({ kind: "more", hiddenCount: 1 });
  });

  it("keeps a stored watermark and turns the tail into a collapse row once everything is shown", () => {
    const expanded = build([workspace(1, tasks(8))], [], { watermarks: new Map([[1, 8]]), mustShowTaskIds: new Set() });
    expect(expanded.watermarks.get(1)).toBe(8);
    expect(expanded.nodes[0].children.at(-1)).toMatchObject({ kind: "more", hiddenCount: 0 });
  });

  it("excludes pinned roots from the page and never limits the pinned group", () => {
    const model = build([workspace(1, tasks(8))], [8, 7, 6, 5, 4, 3], noVisibility);
    const [pinned, ws] = model.nodes;
    expect(pinned.children).toHaveLength(6);
    expect(ids(ws.children)).toEqual(["task:2", "task:1"]);
  });
});
