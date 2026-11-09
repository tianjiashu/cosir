import { describe, expect, it } from "vitest";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { buildTaskForest, forkDescendantIds } from "@/components/task-tree/task-tree-model";

function task(taskId: number, title: string, options: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    task_id: taskId,
    workspace_id: 1,
    title,
    task_type: "user",

    created_at: `2026-01-0${taskId}T00:00:00.000Z`,
    updated_at: `2026-01-0${taskId}T00:00:00.000Z`,
    ...options,
  };
}

function fork(taskId: number, sourceTaskId: number, title = `Fork ${taskId}`): WorkspaceTask {
  return task(taskId, title, {
    task_type: "user_fork",
    extra: { fork: { tree_parent_task_id: sourceTaskId } },
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
    ]);

    expect(roots.map((node) => node.task_id).sort()).toEqual([2, 3, 4]);
    expect(roots.flatMap((node) => node.children)).toEqual([]);
  });
});

describe("task tree title", () => {
  const fullTitle = (title: string) => buildTaskForest([task(1, title)])[0].full_title;

  it("projects capsule tags to readable names", () => {
    expect(fullTitle('<file path="/tmp/notes.md" />帮我优化这个文档')).toBe("notes.md帮我优化这个文档");
    expect(fullTitle('前文 <ref path="src/a.py" lines="12-14" /> 后文')).toBe("前文 src/a.py:12-14 后文");
    expect(fullTitle('<skill name="review-code" />检查')).toBe("review-code检查");
  });

  it("falls back when the title has no visible text", () => {
    expect(fullTitle("   ")).toBe("新对话");
  });

  it("keeps long titles intact; the sidebar ellipsizes them by width", () => {
    const longTitle = "长标题".repeat(30);
    expect(fullTitle(longTitle)).toBe(longTitle);
  });
});

describe("forkDescendantIds", () => {
  it("collects forks of forks but not the node itself", () => {
    const [root] = buildTaskForest([task(1, "源任务"), fork(2, 1), fork(3, 2), fork(4, 1)]);
    expect(forkDescendantIds(root).sort()).toEqual([2, 3, 4]);
    expect(forkDescendantIds(root.children[0])).toEqual([3]);
  });
});
