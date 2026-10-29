import { describe, expect, it } from "vitest";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { buildWorkspaceTreeModel, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "@/components/task-tree/workspace-tree-model";

function task(taskId: number, workspaceId: number, sourceTaskId?: number): WorkspaceTask {
  return {
    task_id: taskId,
    workspace_id: workspaceId,
    title: `Task ${taskId}`,
    task_type: sourceTaskId ? "fork" : "user",
    extra: sourceTaskId ? { fork: { source_task_id: sourceTaskId, source_message_id: "m" } } : null,
    execution_status: null,
    created_at: `2026-01-0${taskId}T00:00:00.000Z`,
    updated_at: `2026-01-0${taskId}T00:00:00.000Z`,
  };
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
});
