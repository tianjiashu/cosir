import { describe, expect, it } from "vitest";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { buildTaskForest } from "@/components/task-tree/task-tree-model";

function task(taskId: number, title: string, options: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    task_id: taskId,
    workspace_id: 1,
    title,
    task_type: "user",
    execution_status: null,

    created_at: `2026-01-0${taskId}T00:00:00.000Z`,
    updated_at: `2026-01-0${taskId}T00:00:00.000Z`,
    ...options,
  };
}

function fork(taskId: number, sourceTaskId: number, title = `Fork ${taskId}`): WorkspaceTask {
  return task(taskId, title, {
    task_type: "fork",
    extra: { fork: { source_task_id: sourceTaskId, source_message_id: `assistant-${taskId + 100}` } },
  });
}

describe("buildTaskForest", () => {
  it("builds an arbitrarily deep Fork tree and preserves sibling order", () => {
    const roots = buildTaskForest([
      fork(4, 2),
      fork(3, 1),
      fork(2, 1),
      fork(5, 2),
      task(1, "Mainline"),
    ]);

    expect(roots.map((node) => node.task_id)).toEqual([1]);
    expect(roots[0].children.map((node) => node.task_id)).toEqual([2, 3]);
    expect(roots[0].children[0].children.map((node) => node.task_id)).toEqual([4, 5]);
  });

  it("keeps missing-source and cyclic Forks visible as roots", () => {
    const roots = buildTaskForest([
      fork(2, 99),
      fork(3, 4),
      fork(4, 3),
      task(5, "Hidden delegation", { task_type: "delegation" }),
    ]);

    expect(roots.map((node) => node.task_id).sort()).toEqual([2, 3, 4]);
    expect(roots.flatMap((node) => node.children)).toEqual([]);
  });
});

describe("task tree title", () => {
  const fullTitle = (title: string) => buildTaskForest([task(1, title)])[0].full_title;

  it("strips inline attachment tokens", () => {
    expect(fullTitle("[[cosir-file:abc]]帮我优化这个文档")).toBe("帮我优化这个文档");
    expect(fullTitle("<!-- [[cosir-file:abc]] -->前文 [[cosir-image:def]]后文")).toBe("前文 后文");
  });

  it("falls back when the title has no visible text", () => {
    expect(fullTitle("[[cosir-file:abc]]")).toBe("新对话");
    expect(fullTitle("   ")).toBe("新对话");
  });

  it("keeps long titles intact; the sidebar ellipsizes them by width", () => {
    const longTitle = "长标题".repeat(30);
    expect(fullTitle(longTitle)).toBe(longTitle);
  });
});
