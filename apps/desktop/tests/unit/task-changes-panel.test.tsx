import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const { useTaskChangesMock } = vi.hoisted(() => ({ useTaskChangesMock: vi.fn() }));

vi.mock("@/components/task-changes/use-task-changes", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/task-changes/use-task-changes")>();
  return { ...actual, useTaskChanges: useTaskChangesMock };
});

import {
  NetDiff,
  parseTaskNetDiff,
  TaskChangeRow,
  TaskChangesPanel,
} from "@/components/task-changes/task-changes-panel";
import {
  isTaskChangesResponseCurrent,
  type TaskChangesViewState,
} from "@/components/task-changes/use-task-changes";
import type { TaskFileChange } from "@/lib/api/changes";

const finalPatch = [
  "diff --git a/src/app.ts b/src/app.ts",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,1 +1,1 @@",
  "-old final baseline",
  "+current final state",
].join("\n");

const change: TaskFileChange = {
  change_id: "chg_app",
  paths: ["src/app.ts"],
  action: "modified",
  status: "pending",
  last_run_id: 18,
  operation_count: 3,
  net_diff: {
    state: "verified",
    additions: 1,
    deletions: 1,
    patch: finalPatch,
    truncated: false,
    has_unrendered_changes: false,
  },
};

function renderRow(isRunActive = false): string {
  return renderToStaticMarkup(
    <TaskChangeRow
      change={change}
      isRunActive={isRunActive}
      actionBusy={false}
      onKeep={() => undefined}
      onRevert={() => undefined}
    />,
  );
}

describe("task ChangeSet panel", () => {
  it("rejects responses after task switches, generation changes, or abort", () => {
    const controller = new AbortController();
    const request = {
      signal: controller.signal,
      requestedTaskId: 12,
      currentTaskId: 12,
      requestedTaskGeneration: 4,
      currentTaskGeneration: 4,
      requestedLoadGeneration: 9,
      currentLoadGeneration: 9,
    };

    expect(isTaskChangesResponseCurrent(request)).toBe(true);
    expect(isTaskChangesResponseCurrent({ ...request, currentTaskId: 13 })).toBe(false);
    expect(isTaskChangesResponseCurrent({ ...request, currentTaskGeneration: 5 })).toBe(false);
    expect(isTaskChangesResponseCurrent({ ...request, currentLoadGeneration: 10 })).toBe(false);
    controller.abort();
    expect(isTaskChangesResponseCurrent(request)).toBe(false);
  });

  it("parses only the final baseline-to-current patch and handles no-net-change and truncation", () => {
    expect(parseTaskNetDiff(change.net_diff).kind).toBe("ready");
    expect(parseTaskNetDiff({ state: "verified", additions: 0, deletions: 0, patch: null, truncated: false, has_unrendered_changes: false })).toEqual({ kind: "empty" });
    expect(parseTaskNetDiff({ state: "verified", additions: 0, deletions: 0, patch: "diff --git a/old.ts b/new.ts\nsimilarity index 100%\nrename from old.ts\nrename to new.ts", truncated: false, has_unrendered_changes: true })).toEqual({ kind: "empty" });
    expect(parseTaskNetDiff({ state: "verified", additions: 5, deletions: 2, patch: finalPatch, truncated: true, has_unrendered_changes: false })).toEqual({ kind: "unavailable", reason: "truncated" });
    expect(parseTaskNetDiff({ state: "conflict", additions: 0, deletions: 0, patch: null, truncated: false, has_unrendered_changes: false })).toEqual({ kind: "unavailable", reason: "missing" });
  });

  it("renders every file in a multi-file final net diff", () => {
    const secondPatch = [
      "diff --git a/src/other.ts b/src/other.ts",
      "--- a/src/other.ts",
      "+++ b/src/other.ts",
      "@@ -1,1 +1,1 @@",
      "-old",
      "+new",
    ].join("\n");
    const parsed = parseTaskNetDiff({
      state: "verified",
      additions: 2,
      deletions: 2,
      patch: `${finalPatch}\n${secondPatch}`,
      truncated: false,
      has_unrendered_changes: false,
    });

    expect(parsed.kind).toBe("ready");
    if (parsed.kind === "ready") expect(parsed.files).toHaveLength(2);
  });

  it("renders one file group with one baseline revert action, regardless of operation count", () => {
    const html = renderRow();

    expect(html).toContain("修改 · 3 次操作");
    expect(html).toContain("回退到基线");
    expect(html).toContain("保留当前状态");
    expect(html).not.toContain("逐步");
    expect(html).not.toContain("撤销第");
  });

  it("does not display a multi-path directory change as a file move", () => {
    const html = renderToStaticMarkup(
      <TaskChangeRow
        change={{ ...change, action: "deleted", paths: ["src/tree", "src/tree/a.ts"] }}
        isRunActive={false}
        actionBusy={false}
        onKeep={() => undefined}
        onRevert={() => undefined}
      />,
    );
    expect(html).toContain("src/tree（共 2 个路径）");
    expect(html).not.toContain("src/tree → src/tree/a.ts");
  });

  it("distinguishes non-text baseline changes from a true no-op", () => {
    const nonTextHtml = renderToStaticMarkup(
      <NetDiff
        diff={{
          state: "verified",
          additions: 0,
          deletions: 0,
          patch: null,
          truncated: false,
          has_unrendered_changes: true,
        }}
      />,
    );
    expect(nonTextHtml).toContain("没有可显示的文本行 Diff");
    expect(nonTextHtml).not.toContain("与最近基线无差异");
  });

  it("suppresses deleted content while keeping the deletion summary and counts", () => {
    const html = renderToStaticMarkup(
      <NetDiff
        contentSuppressed
        diff={{
          state: "verified",
          additions: 0,
          deletions: 1,
          patch: null,
          truncated: false,
          has_unrendered_changes: false,
        }}
      />,
    );

    expect(html).toContain("该文件已删除");
    expect(html).not.toContain("最终 Diff 暂不可用");
    expect(html).not.toContain("没有可显示的文本行 Diff");
  });

  it("shows controlled per-file action feedback without exposing arbitrary server messages", () => {
    const html = renderToStaticMarkup(
      <TaskChangeRow
        change={change}
        isRunActive={false}
        actionBusy={false}
        result={{ change_id: change.change_id, outcome: "conflict", message: "raw server detail" }}
        onKeep={() => undefined}
        onRevert={() => undefined}
      />,
    );

    expect(html).toContain("文件状态与变更记录不一致，本次未回退");
    expect(html).not.toContain("raw server detail");
  });

  it("shows successful and conflicting results together after a partial batch action", () => {
    const view: TaskChangesViewState = {
      changeSet: {
        task_id: 12,
        files: [{ ...change, change_id: "chg_conflict", paths: ["conflict.ts"] }],
      },
      loading: false,
      loadError: null,
      actionError: null,
      refreshing: false,
      actionBusy: false,
      resultsByChangeId: new Map([
        ["chg_success", { change_id: "chg_success", outcome: "reverted" }],
        ["chg_conflict", { change_id: "chg_conflict", outcome: "conflict" }],
      ]),
      refresh: async () => undefined,
      keep: async () => undefined,
      revert: async () => undefined,
    };
    useTaskChangesMock.mockReturnValue(view);

    const html = renderToStaticMarkup(<TaskChangesPanel taskId={12} isRunActive={false} />);

    expect(html).toContain("1 个待处理文件组");
    expect(html).toContain("conflict.ts");
    expect(html).toContain("文件状态与变更记录不一致，本次未回退");
    expect(html).toContain("操作结果");
    expect(html).toContain("chg_success");
    expect(html).toContain("已回退到基线");
  });

  it("disables Keep and Revert during a Run", () => {
    const activeRunHtml = renderRow(true);

    expect(activeRunHtml.match(/<button[^>]*\sdisabled=""/g)).toHaveLength(2);
  });

  it("shows terminal coverage disclosure", () => {
    const html = renderToStaticMarkup(<TaskChangesPanel taskId={12} isRunActive={false} />);
    expect(html).toContain("当前追踪文件工具修改；终端命令可能产生未记录的文件变更。");
  });
});
