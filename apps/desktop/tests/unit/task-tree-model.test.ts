import { describe, expect, it } from "vitest";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import {
  TASK_TITLE_DISPLAY_LIMIT,
  buildTaskTreeModel,
  getTaskAncestorIds,
  taskDisplayTitle,
} from "@/components/task-tree/task-tree-model";

function task(taskId: number, title: string, options: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    task_id: taskId,
    workspace_id: 1,
    title,
    task_type: "user",
    fork_available: true,
    execution_status: null,
    context_usage_used: null,
    context_window_total: null,
    created_at: `2026-01-0${taskId}T00:00:00.000Z`,
    updated_at: `2026-01-0${taskId}T00:00:00.000Z`,
    ...options,
  };
}

function fork(taskId: number, sourceTaskId: number, title = `Fork ${taskId}`): WorkspaceTask {
  return task(taskId, title, {
    task_type: "fork",
    extra: { fork: { source_task_id: sourceTaskId, source_run_id: taskId + 100 } },
  });
}

describe("buildTaskTreeModel", () => {
  it("builds an arbitrarily deep Fork tree and preserves sibling order", () => {
    const model = buildTaskTreeModel([
      fork(4, 2),
      fork(3, 1),
      fork(2, 1),
      fork(5, 2),
      task(1, "Mainline"),
    ]);

    expect(model.roots.map((node) => node.task_id)).toEqual([1]);
    expect(model.roots[0].children.map((node) => node.task_id)).toEqual([2, 3]);
    expect(model.roots[0].children[0].children.map((node) => node.task_id)).toEqual([4, 5]);
    expect(getTaskAncestorIds(5, model.parentById)).toEqual(new Set([2, 1]));
  });

  it("keeps missing-source and cyclic Forks visible as roots", () => {
    const model = buildTaskTreeModel([
      fork(2, 99),
      fork(3, 4),
      fork(4, 3),
      task(5, "Hidden delegation", { task_type: "delegation" }),
    ]);

    expect(model.roots.map((node) => node.task_id).sort()).toEqual([2, 3, 4]);
    expect(model.nodesById.has(5)).toBe(false);
  });
});

describe("task display title", () => {
  it("strips inline attachment tokens before measuring length", () => {
    expect(taskDisplayTitle("[[cosir-file:abc]]帮我优化这个文档")).toBe("帮我优化这个文档");
    expect(taskDisplayTitle("<!-- [[cosir-file:abc]] -->前文 [[cosir-image:def]]后文")).toBe("前文 后文");
  });

  it("falls back when the title has no visible text", () => {
    expect(taskDisplayTitle("[[cosir-file:abc]]")).toBe("新对话");
    expect(taskDisplayTitle("   ")).toBe("新对话");
  });

  it("truncates long titles with a single ellipsis", () => {
    expect(taskDisplayTitle("x".repeat(TASK_TITLE_DISPLAY_LIMIT + 5))).toBe(
      `${"x".repeat(TASK_TITLE_DISPLAY_LIMIT - 1)}…`,
    );
    expect(taskDisplayTitle("短标题", 2)).toBe("短…");
  });

  it("does not stack an ellipsis onto a trailing ASCII dot", () => {
    // 后端标题以 "..." 结尾时，切点可能落在点号上。
    expect(taskDisplayTitle(`${"x".repeat(TASK_TITLE_DISPLAY_LIMIT - 1)}..`)).toBe(
      `${"x".repeat(TASK_TITLE_DISPLAY_LIMIT - 1)}…`,
    );
  });

  it("exposes both display and full titles on tree nodes", () => {
    const visibleTitle = "长标题".repeat(20);
    const model = buildTaskTreeModel([task(1, `[[cosir-file:abc]]${visibleTitle}`)]);
    const node = model.roots[0];

    expect(node.full_title).toBe(visibleTitle);
    expect(node.display_title).toBe(`${visibleTitle.slice(0, TASK_TITLE_DISPLAY_LIMIT - 1)}…`);
  });
});
